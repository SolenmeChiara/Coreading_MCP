"""共读书房 Co-Reading Room — RAG 向量检索模块

职责：
- Embedding provider 抽象（Ollama / 云端 OpenAI 兼容）
- 中文段落级分块（带重叠）
- 索引构建与增量更新
- 混合检索（FTS5 + 向量）+ 降级
- 证据格式化
"""

import struct
import logging
import asyncio
from typing import Optional

from config import (
    EMBEDDING_API_BASE, EMBEDDING_API_KEY, EMBEDDING_MODEL,
    EMBEDDING_CHUNK_SIZE, EMBEDDING_CHUNK_OVERLAP,
)
from database import Database

logger = logging.getLogger("co-reading.embedding")

# 句末标点
_SENTENCE_ENDINGS = set("。！？.!?\n")


# ========== 向量序列化 ==========

def _encode_vector(vec: list[float]) -> bytes:
    """float list → bytes (little-endian float32)"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _decode_vector(data: bytes) -> list[float]:
    """bytes → float list"""
    n = len(data) // 4
    return list(struct.unpack(f"<{n}f", data))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ========== 分块 ==========

def chunk_text(text: str, chunk_size: int = EMBEDDING_CHUNK_SIZE,
               overlap: int = EMBEDDING_CHUNK_OVERLAP) -> list[str]:
    """
    中文段落级分块。优先在段落/句子边界切分，带重叠。
    返回 chunk 列表。
    """
    text = text.strip()
    if not text:
        return []

    # 先按段落切
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]

    chunks = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 1 <= chunk_size:
            current = (current + "\n" + para).strip() if current else para
        else:
            if current:
                chunks.append(current)
            # 如果单段超长，按句子切
            if len(para) > chunk_size:
                sub_chunks = _split_long_paragraph(para, chunk_size)
                chunks.extend(sub_chunks[:-1])
                current = sub_chunks[-1] if sub_chunks else ""
            else:
                # 重叠：取上一个 chunk 的尾部
                if chunks and overlap > 0:
                    tail = chunks[-1][-overlap:]
                    current = tail + "\n" + para
                else:
                    current = para

    if current:
        chunks.append(current)

    return chunks


def _split_long_paragraph(text: str, max_len: int) -> list[str]:
    """按句子边界切分超长段落"""
    parts = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        if end < len(text):
            # 向回找句子边界
            for i in range(end - 1, max(start, end - 200) - 1, -1):
                if text[i] in _SENTENCE_ENDINGS:
                    end = i + 1
                    break
        parts.append(text[start:end].strip())
        start = end
    return [p for p in parts if p]


# ========== Embedding Provider ==========

def _get_embedding_config(db: Database) -> dict:
    """获取 embedding 模型配置。DB settings 优先，其次环境变量/config.py"""
    settings = db.get_all_settings()
    return {
        "api_base": settings.get("embedding_api_base", EMBEDDING_API_BASE),
        "api_key": settings.get("embedding_api_key", EMBEDDING_API_KEY),
        "model": settings.get("embedding_model", EMBEDDING_MODEL),
    }


def _call_embedding_api(config: dict, texts: list[str]) -> Optional[list[list[float]]]:
    """
    调用 OpenAI 兼容 embedding API。同步函数，在线程池中调用。
    返回 embedding 向量列表，失败返回 None。
    """
    if not config["api_key"]:
        logger.info("No embedding API key configured, skipping")
        return None

    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=config["api_base"],
            api_key=config["api_key"],
        )

        response = client.embeddings.create(
            model=config["model"],
            input=texts,
        )

        return [item.embedding for item in response.data]

    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return None
    except Exception as e:
        logger.error("Embedding API call failed: %s", e)
        return None


# ========== 索引管理 ==========

async def index_page(db: Database, book_id: int, page_number: int) -> bool:
    """为某页构建 embedding 索引。异步，在线程池中调 API。返回成功与否。"""
    page = db.get_page(book_id, page_number)
    if not page:
        return False

    content = page["content"]
    # 写作模式下用草稿内容
    book = db.get_book(book_id)
    if book and book.get("mode") == "write":
        draft = db.get_draft_page(book_id, page_number)
        if draft:
            content = draft["content"]

    chunks = chunk_text(content)
    if not chunks:
        return False

    config = _get_embedding_config(db)
    vectors = await asyncio.to_thread(_call_embedding_api, config, chunks)

    if not vectors or len(vectors) != len(chunks):
        logger.warning("Embedding failed or mismatch for book %d page %d", book_id, page_number)
        return False

    chunk_data = []
    for i, (text, vec) in enumerate(zip(chunks, vectors)):
        chunk_data.append({
            "chunk_index": i,
            "content": text,
            "embedding": _encode_vector(vec),
        })

    db.upsert_embeddings(book_id, page_number, chunk_data)
    logger.info("Indexed book %d page %d: %d chunks", book_id, page_number, len(chunk_data))
    return True


async def index_book(db: Database, book_id: int) -> dict:
    """为整本书构建索引。返回 {"indexed": int, "failed": int, "total": int}。"""
    book = db.get_book(book_id)
    if not book:
        return {"indexed": 0, "failed": 0, "total": 0}

    total = book["total_pages"]
    indexed = 0
    failed = 0

    for pn in range(1, total + 1):
        try:
            ok = await index_page(db, book_id, pn)
            if ok:
                indexed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Index failed for book %d page %d: %s", book_id, pn, e)
            failed += 1

    logger.info("Indexed book %d: %d/%d pages (failed: %d)", book_id, indexed, total, failed)
    return {"indexed": indexed, "failed": failed, "total": total}


# ========== 混合检索 ==========

async def hybrid_search(
    db: Database,
    book_id: int,
    query: str,
    top_k: int = 5,
) -> list[dict]:
    """
    混合检索：FTS5 + 向量双路召回 → 重排去重。
    每条结果带 book_id/page_number/chunk_id/content/score/source。
    失败时降级到 FTS5 或返回空。
    """
    results = []

    # === 路径 1: FTS5 / LIKE 文本检索 ===
    fts_results = _fts_search(db, book_id, query, top_k * 2)

    # === 路径 2: 向量检索 ===
    vec_results = await _vector_search(db, book_id, query, top_k * 2)

    # === 合并去重 ===
    seen = set()
    combined = []

    for r in vec_results:
        key = (r["page_number"], r.get("chunk_index", -1))
        if key not in seen:
            seen.add(key)
            combined.append(r)

    for r in fts_results:
        key = (r["page_number"], r.get("chunk_index", -1))
        if key not in seen:
            seen.add(key)
            combined.append(r)

    # 按 score 降序排序
    combined.sort(key=lambda x: x.get("score", 0), reverse=True)

    return combined[:top_k]


def _fts_search(db: Database, book_id: int, query: str, limit: int) -> list[dict]:
    """FTS5 文本检索。FTS5 不可用时降级到 LIKE。"""
    try:
        fts_results = db.search_memory(book_id, query)
        results = []
        for r in fts_results[:limit]:
            results.append({
                "book_id": book_id,
                "page_number": r["page_number"],
                "chunk_index": -1,
                "content": r["summary"],
                "score": 0.5,  # FTS 默认置信度
                "source": "fts",
                "keywords": r.get("keywords", ""),
            })
        return results
    except Exception as e:
        logger.warning("FTS search failed: %s", e)
        return []


async def _vector_search(
    db: Database, book_id: int, query: str, limit: int,
) -> list[dict]:
    """向量检索。embedding 服务不可用时返回空列表（降级）。"""
    try:
        config = _get_embedding_config(db)
        query_vec_list = await asyncio.to_thread(_call_embedding_api, config, [query])
        if not query_vec_list:
            logger.info("Vector search degraded: embedding API unavailable")
            return []

        query_vec = query_vec_list[0]

        # 获取所有 embeddings 并计算相似度
        all_embs = db.get_all_embeddings(book_id)
        if not all_embs:
            logger.info("Vector search: no embeddings for book %d", book_id)
            return []

        scored = []
        for emb in all_embs:
            if not emb.get("embedding"):
                continue
            doc_vec = _decode_vector(emb["embedding"])
            score = _cosine_similarity(query_vec, doc_vec)
            scored.append({
                "book_id": book_id,
                "page_number": emb["page_number"],
                "chunk_index": emb["chunk_index"],
                "content": emb["content"],
                "score": round(score, 4),
                "source": "vector",
            })

        scored.sort(key=lambda x: x["score"], reverse=True)

        # 过滤低置信度（< 0.3）
        scored = [s for s in scored if s["score"] >= 0.3]

        return scored[:limit]

    except Exception as e:
        logger.warning("Vector search failed (degrading): %s", e)
        return []


# ========== 证据格式化 ==========

def format_evidence(results: list[dict]) -> str:
    """将检索结果格式化为可追溯的证据文本"""
    if not results:
        return "未找到可靠证据。"

    lines = []
    for i, r in enumerate(results, 1):
        source_tag = "FTS" if r["source"] == "fts" else f"向量({r['score']:.2f})"
        content_preview = r["content"][:150].replace("\n", " ")
        lines.append(
            f"{i}. [第 {r['page_number']} 页] ({source_tag}) {content_preview}"
        )

    return "\n".join(lines)

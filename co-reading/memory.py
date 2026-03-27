"""共读书房 Co-Reading Room — 上下文管理和记忆压缩"""

import json
import base64
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import (
    RECENT_PAGES_FULL, SUMMARY_PAGES_RANGE,
    SUMMARY_API_BASE, SUMMARY_API_KEY, SUMMARY_MODEL, SUMMARY_PROMPT,
    SCREENSHOT_DIR,
)
from database import Database
from parser import split_paragraphs

logger = logging.getLogger("co-reading.memory")


# ========== 上下文拼装 ==========

def assemble_context(db: Database, book_id: int) -> Optional[dict]:
    """
    拼装 read_current_page 的完整上下文。

    返回结构:
    {
        "book": {...},
        "current_page": {"number", "content", "paragraphs", "annotations"},
        "recent_pages": [{"number", "content", "annotations"}],
        "memory_pages": [{"number", "summary", "keywords"}],
        "time_context": {"since_last_read", "session_duration"},
        "unread_messages": [...]
    }
    """
    book = db.get_book(book_id)
    if not book:
        return None

    page_num = book["current_page"]

    # 当前页：完整文本 + 段落 + 批注
    page = db.get_page(book_id, page_num)
    if not page:
        return None

    paragraphs = split_paragraphs(page["content"])
    annotations = db.get_annotations(book_id, page_num)

    current_page = {
        "number": page_num,
        "content": page["content"],
        "paragraphs": paragraphs,
        "annotations": annotations,
        "screenshot_path": page.get("screenshot_path"),
    }

    # 最近 N 页：完整文本 + 批注
    recent_pages = []
    for pn in range(max(1, page_num - RECENT_PAGES_FULL), page_num):
        rp = db.get_page(book_id, pn)
        if rp:
            recent_pages.append({
                "number": pn,
                "content": rp["content"],
                "annotations": db.get_annotations(book_id, pn),
            })

    # 第 4~20 页前：仅摘要 + 关键词
    memory_start = max(1, page_num - SUMMARY_PAGES_RANGE)
    memory_end = max(0, page_num - RECENT_PAGES_FULL - 1)
    memory_pages = []
    if memory_end >= memory_start:
        memory_pages = db.get_memories_range(book_id, memory_start, memory_end)

    # 时间上下文
    time_context = _build_time_context(db, book)

    # 未读聊天消息
    unread_messages = db.get_unread_messages(book_id)

    return {
        "book": book,
        "current_page": current_page,
        "recent_pages": recent_pages,
        "memory_pages": memory_pages,
        "time_context": time_context,
        "unread_messages": unread_messages,
    }


def _build_time_context(db: Database, book: dict) -> dict:
    """构建时间上下文信息"""
    now = datetime.now(timezone.utc)
    result = {}

    # 距上次阅读多久
    if book.get("last_read_at"):
        try:
            last_read = datetime.fromisoformat(book["last_read_at"].replace("Z", "+00:00"))
            if last_read.tzinfo is None:
                last_read = last_read.replace(tzinfo=timezone.utc)
            delta = now - last_read
            result["since_last_read"] = _format_duration(delta.total_seconds())
        except (ValueError, TypeError):
            result["since_last_read"] = "未知"
    else:
        result["since_last_read"] = "首次阅读"

    # 本次会话时长
    session = db.get_active_session(book["id"])
    if session and session.get("started_at"):
        try:
            started = datetime.fromisoformat(session["started_at"].replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            delta = now - started
            result["session_duration"] = _format_duration(delta.total_seconds())
        except (ValueError, TypeError):
            result["session_duration"] = "未知"
    else:
        result["session_duration"] = None

    return result


def _format_duration(seconds: float) -> str:
    """将秒数格式化为人类可读的时间描述"""
    seconds = abs(seconds)
    if seconds < 60:
        return "刚刚"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = int(minutes // 60)
    remaining_mins = minutes % 60
    if hours < 24:
        if remaining_mins:
            return f"{hours} 小时 {remaining_mins} 分钟"
        return f"{hours} 小时"
    days = int(hours // 24)
    return f"{days} 天"


# ========== 格式化为 MCP 输出 ==========

def format_context_for_mcp(context: dict, user_name: str) -> str:
    """将拼装好的上下文格式化为 Markdown 字符串，供 read_current_page 返回"""
    book = context["book"]
    cp = context["current_page"]
    lines = []

    # 标题
    lines.append(f"**{book['title']}** — 第 {cp['number']}/{book['total_pages']} 页")
    lines.append("")

    # 当前页段落（带编号）
    for i, p in enumerate(cp["paragraphs"]):
        lines.append(f"[{i}] {p}")
    lines.append("")
    lines.append("---")

    # 当前页批注
    if cp["annotations"]:
        lines.append(f"\n**本页批注 ({len(cp['annotations'])} 条):**\n")
        for a in cp["annotations"]:
            lines.append(_format_annotation(a, user_name))
    else:
        lines.append("\n*这一页还没有批注。*")

    # 最近翻过的页面
    if context["recent_pages"]:
        lines.append("\n---")
        lines.append("\n**最近翻过的页面:**\n")
        for rp in reversed(context["recent_pages"]):
            lines.append(f"### 第 {rp['number']} 页")
            # 给近页也做段落编号，方便引用
            paras = split_paragraphs(rp["content"])
            for i, p in enumerate(paras):
                lines.append(f"[{i}] {p}")
            if rp["annotations"]:
                lines.append(f"\n*批注 ({len(rp['annotations'])} 条):*")
                for a in rp["annotations"]:
                    lines.append(_format_annotation(a, user_name))
            lines.append("")

    # 早期阅读记忆
    if context["memory_pages"]:
        lines.append("---")
        lines.append("\n**早期阅读记忆:**\n")
        for m in context["memory_pages"]:
            kw = m.get("keywords") or ""
            kw_str = f" (关键词: {kw})" if kw else ""
            lines.append(f"- 第 {m['page_number']} 页: {m['summary']}{kw_str}")
        lines.append("")

    # 时间上下文
    tc = context["time_context"]
    lines.append("---")
    lines.append(f"\n**时间:** 距上次阅读: {tc['since_last_read']}")
    if tc.get("session_duration"):
        lines.append(f"本次阅读时长: {tc['session_duration']}")

    # 未读留言
    unread = context["unread_messages"]
    if unread:
        lines.append(f"\n---")
        lines.append(f"\n**{user_name} 的留言 ({len(unread)} 条待回复):**\n")
        for msg in unread:
            hl = f"「{msg['highlight_text']}」" if msg.get("highlight_text") else ""
            lines.append(f"- {hl}{msg['content']}  *(第 {msg['page_number']} 页)*")
        lines.append(f"\n*用 reply 工具回复 {user_name} 的留言。*")

    return "\n".join(lines)


def get_screenshot_base64(screenshot_path: Optional[str]) -> Optional[str]:
    """读取截图文件并返回 base64 编码字符串，失败返回 None"""
    if not screenshot_path:
        return None
    full_path = SCREENSHOT_DIR / screenshot_path
    if not full_path.exists():
        return None
    try:
        return base64.b64encode(full_path.read_bytes()).decode("ascii")
    except Exception as e:
        logger.warning("Failed to read screenshot %s: %s", screenshot_path, e)
        return None


def _format_annotation(a: dict, user_name: str) -> str:
    """格式化单条批注"""
    author_label = "Claude" if a["author"] == "claude" else user_name
    aid = f"[ID:{a['id']}] " if a.get("id") else ""
    # 位置标注
    target = a.get("target_type", "paragraph")
    if target == "region" and a.get("bbox_x0") is not None:
        loc = f"[区域 ({a['bbox_x0']:.2f},{a['bbox_y0']:.2f})-({a['bbox_x1']:.2f},{a['bbox_y1']:.2f})] "
    elif a.get("paragraph_index") is not None:
        loc = f"[段落 {a['paragraph_index']}] "
    else:
        loc = ""
    hl = f"「{a['highlight_text']}」" if a.get("highlight_text") else ""
    return f"- {aid}{loc}{hl}**{author_label}**: {a['content']}"


# ========== 记忆压缩 ==========

def _get_summary_config(db: Database) -> dict:
    """获取记忆压缩的模型配置。DB settings 优先，其次环境变量/config.py 默认值"""
    settings = db.get_all_settings()
    return {
        "api_base": settings.get("summary_api_base", SUMMARY_API_BASE),
        "api_key": settings.get("summary_api_key", SUMMARY_API_KEY),
        "model": settings.get("summary_model", SUMMARY_MODEL),
    }


async def compress_page_memory(db: Database, book_id: int, page_number: int) -> bool:
    """
    异步生成某页的记忆摘要。翻页时调用，不阻塞主流程。
    返回 True 表示成功，False 表示失败（但不抛异常）。
    """
    try:
        page = db.get_page(book_id, page_number)
        if not page:
            logger.warning("Cannot compress: page %d not found for book %d", page_number, book_id)
            return False

        annotations = db.get_annotations(book_id, page_number)
        anno_text = "\n".join(
            f"- {a['author']}: {a['content']}" for a in annotations
        ) if annotations else "（无批注）"

        prompt = SUMMARY_PROMPT.format(
            page_content=page["content"],
            annotations=anno_text,
        )

        config = _get_summary_config(db)
        if not config["api_key"]:
            logger.info("No summary API key configured, skipping memory compression for page %d", page_number)
            return False

        # 使用 openai SDK（兼容任意 OpenAI 格式端点）
        # 在线程池中执行，避免阻塞事件循环
        result = await asyncio.to_thread(_call_summary_api, config, prompt)

        if result:
            summary = result.get("summary", "")
            keywords_list = result.get("keywords", [])
            keywords = ", ".join(keywords_list) if isinstance(keywords_list, list) else str(keywords_list)
            db.upsert_memory(book_id, page_number, summary, keywords)
            logger.info("Compressed memory for book %d page %d", book_id, page_number)
            return True
        else:
            logger.warning("Empty result from summary API for book %d page %d", book_id, page_number)
            return False

    except Exception as e:
        logger.error("Memory compression failed for book %d page %d: %s", book_id, page_number, e)
        return False


def _call_summary_api(config: dict, prompt: str) -> Optional[dict]:
    """调用 OpenAI 兼容 API 生成摘要。同步函数，在线程池中调用。"""
    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=config["api_base"],
            api_key=config["api_key"],
        )

        response = client.chat.completions.create(
            model=config["model"],
            messages=[
                {"role": "system", "content": "你是一个阅读笔记助手。请严格按 JSON 格式返回结果。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
        )

        content = response.choices[0].message.content or ""
        # 尝试从返回内容中提取 JSON
        content = content.strip()
        # 处理可能被 markdown 代码块包裹的 JSON
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        return json.loads(content)

    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return None
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse summary JSON: %s", e)
        return None
    except Exception as e:
        logger.error("Summary API call failed: %s", e)
        return None

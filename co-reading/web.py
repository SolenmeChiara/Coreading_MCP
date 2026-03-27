"""共读书房 Co-Reading Room — FastAPI HTTP 服务器"""

import sys
import json
import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response
from pydantic import BaseModel
import uvicorn

from config import (
    DB_PATH, UPLOAD_DIR, SCREENSHOT_DIR, FRONTEND_DIR, HTTP_HOST, HTTP_PORT,
    CHARS_PER_PAGE, MAX_UPLOAD_SIZE, USER_NAME,
    SUMMARY_API_BASE, SUMMARY_API_KEY, SUMMARY_MODEL,
    EMBEDDING_API_BASE, EMBEDDING_API_KEY, EMBEDDING_MODEL,
)
from database import Database
from parser import parse_txt, parse_pdf, split_paragraphs

# ---------- 日志 ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(Path(__file__).parent / "logs" / "web.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("co-reading.web")

# ---------- App ----------

app = FastAPI(title="Co-Reading Room", version="0.3.0")
db = Database(DB_PATH)


# ---------- 首次启动：导入欢迎文本 ----------

def _seed_welcome_book():
    """如果书架为空，自动导入欢迎文本"""
    if db.list_books():
        return
    welcome_path = Path(__file__).parent / "data" / "welcome.txt"
    if not welcome_path.exists():
        return
    try:
        pages = parse_txt(welcome_path, CHARS_PER_PAGE)
        if not pages:
            return
        dest = UPLOAD_DIR / "welcome.txt"
        shutil.copy2(welcome_path, dest)
        book_id = db.add_book("共读的意义", "welcome.txt", "txt", len(pages))
        db.add_pages_bulk(book_id, [(i + 1, p) for i, p in enumerate(pages)])
        logger.info("Seeded welcome book (%d pages)", len(pages))
    except Exception as e:
        logger.warning("Failed to seed welcome book: %s", e)

_seed_welcome_book()


# ---------- Request models ----------

class ProgressUpdate(BaseModel):
    page: int

class AnnotationCreate(BaseModel):
    content: str
    paragraph_index: Optional[int] = None
    highlight_text: Optional[str] = None
    target_type: str = "paragraph"          # paragraph | region | page_note
    bbox_x0: Optional[float] = None
    bbox_y0: Optional[float] = None
    bbox_x1: Optional[float] = None
    bbox_y1: Optional[float] = None

class ChatMessageCreate(BaseModel):
    content: str
    highlight_text: Optional[str] = None

class SessionEnd(BaseModel):
    session_id: int
    pages_read: int = 0

class SettingsUpdate(BaseModel):
    settings: dict

class ModeUpdate(BaseModel):
    mode: str

class DraftUpdate(BaseModel):
    content: str
    expected_revision: Optional[int] = None

class VersionCreate(BaseModel):
    label: Optional[str] = None


# ========== API 端点 ==========

# ----- 文档管理 -----

_ALLOWED_EXTENSIONS = {".txt", ".pdf"}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "Missing filename")

    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"仅支持 {', '.join(_ALLOWED_EXTENSIONS)} 文件")

    # 读取内容并检查大小
    content_bytes = await file.read()
    if len(content_bytes) > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_SIZE // (1024*1024)}MB 限制")

    # 保存原始文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_filename = f"{timestamp}_{file.filename}"
    file_path = UPLOAD_DIR / safe_filename
    file_path.write_bytes(content_bytes)

    title = Path(file.filename).stem
    file_type = ext.lstrip(".")

    try:
        if file_type == "txt":
            pages = parse_txt(file_path, CHARS_PER_PAGE)
            if not pages:
                file_path.unlink(missing_ok=True)
                raise HTTPException(400, "文件内容为空或无法解析")
            book_id = db.add_book(title, safe_filename, "txt", len(pages))
            db.add_pages_bulk(book_id, [(i + 1, p) for i, p in enumerate(pages)])

        elif file_type == "pdf":
            # 先获取页数
            import fitz
            doc = fitz.open(str(file_path))
            total_pages = doc.page_count
            doc.close()
            if total_pages == 0:
                file_path.unlink(missing_ok=True)
                raise HTTPException(400, "PDF 文件为空")

            # 创建书记录（需要 book_id 来存截图）
            book_id = db.add_book(title, safe_filename, "pdf", total_pages)

            # 解析 PDF + 生成截图 + 文本块
            pages_data = parse_pdf(file_path, SCREENSHOT_DIR, book_id)
            db.add_pages_bulk(book_id, [
                (p["page_number"], p["content"], p["screenshot_path"])
                for p in pages_data
            ])
            # 存储结构化文本块
            for p in pages_data:
                if p.get("text_blocks"):
                    db.add_text_blocks_bulk(book_id, p["page_number"], p["text_blocks"])
            total_pages = len(pages_data)

    except HTTPException:
        raise
    except Exception as e:
        # 清理失败的上传
        file_path.unlink(missing_ok=True)
        logger.error("Upload failed for '%s': %s", title, e)
        raise HTTPException(500, f"文件解析失败: {e}")

    final_pages = total_pages if file_type == "pdf" else len(pages)
    logger.info("Uploaded '%s' (%s): %d pages", title, file_type, final_pages)
    return {"id": book_id, "title": title, "total_pages": final_pages, "file_type": file_type}


@app.get("/api/books")
async def list_books():
    return db.list_books()


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: int):
    if not db.delete_book_cascade(book_id):
        raise HTTPException(404, "Book not found")
    return JSONResponse(status_code=200, content={"message": "Deleted"})


# ----- 阅读 -----

@app.get("/api/books/{book_id}/page/{page_num}")
async def get_page(book_id: int, page_num: int):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if page_num < 1 or page_num > book["total_pages"]:
        raise HTTPException(400, f"页码超出范围 (1-{book['total_pages']})")

    page = db.get_page(book_id, page_num)
    if not page:
        raise HTTPException(404, "Page not found")

    paragraphs = split_paragraphs(page["content"])

    # 截图 URL（PDF 才有）
    screenshot_url = None
    if page.get("screenshot_path"):
        screenshot_url = f"/api/screenshots/{page['screenshot_path']}"

    # 文本块（PDF 才有，bbox 已归一化为 0~1 不需要绝对尺寸）
    text_blocks = []
    if book["file_type"] == "pdf":
        text_blocks = db.get_text_blocks(book_id, page_num)

    return {
        "book_id": book_id,
        "page_number": page_num,
        "total_pages": book["total_pages"],
        "current_page": book["current_page"],
        "title": book["title"],
        "file_type": book["file_type"],
        "mode": book.get("mode", "read"),
        "paragraphs": paragraphs,
        "raw_content": page["content"],
        "screenshot_url": screenshot_url,
        "text_blocks": text_blocks,
    }


@app.post("/api/books/{book_id}/progress")
async def update_progress(book_id: int, body: ProgressUpdate):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if body.page < 1 or body.page > book["total_pages"]:
        raise HTTPException(400, "页码超出范围")
    db.update_progress(book_id, body.page)
    return {"message": "OK"}


@app.get("/api/books/{book_id}/current-progress")
async def get_current_progress(book_id: int):
    """轻量级端点，前端轮询用来检测 Claude 是否通过 MCP 翻了页"""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    return {
        "current_page": book["current_page"],
        "last_read_at": book["last_read_at"],
    }


# ----- 截图 -----

@app.get("/api/screenshots/{book_id}/{filename}")
async def serve_screenshot(book_id: int, filename: str):
    """提供 PDF 页面截图"""
    path = (SCREENSHOT_DIR / str(book_id) / filename).resolve()
    if not path.is_relative_to(SCREENSHOT_DIR.resolve()) or not path.exists():
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(path, media_type="image/png")


# ----- 批注 -----

def _validate_bbox(target_type: str, x0, y0, x1, y1) -> Optional[str]:
    """校验 bbox 与 target_type 的一致性。返回 None 表示合法，否则返回错误信息。"""
    has_bbox = any(v is not None for v in (x0, y0, x1, y1))
    if target_type == "region":
        if not all(v is not None for v in (x0, y0, x1, y1)):
            return "target_type=region 时 bbox 四个坐标必须全部提供"
        for name, v in [("bbox_x0", x0), ("bbox_y0", y0), ("bbox_x1", x1), ("bbox_y1", y1)]:
            if not (0.0 <= v <= 1.0):
                return f"{name}={v} 超出范围，必须在 [0, 1] 之间"
        if x0 >= x1 or y0 >= y1:
            return f"bbox 坐标无效：需要 x0<x1 且 y0<y1"
    else:
        if has_bbox:
            return f"target_type={target_type} 时不应提供 bbox 坐标"
    return None


@app.get("/api/books/{book_id}/page/{page_num}/annotations")
async def get_annotations(book_id: int, page_num: int):
    return db.get_annotations(book_id, page_num)


@app.post("/api/books/{book_id}/page/{page_num}/annotations")
async def add_annotation(book_id: int, page_num: int, body: AnnotationCreate):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if page_num < 1 or page_num > book["total_pages"]:
        raise HTTPException(400, "页码超出范围")

    # bbox 合法性校验
    bbox_err = _validate_bbox(body.target_type, body.bbox_x0, body.bbox_y0, body.bbox_x1, body.bbox_y1)
    if bbox_err:
        raise HTTPException(422, bbox_err)

    bbox = None
    if body.target_type == "region":
        bbox = (body.bbox_x0, body.bbox_y0, body.bbox_x1, body.bbox_y1)

    annotation_id = db.add_annotation(
        book_id=book_id,
        page_number=page_num,
        author="user",
        content=body.content,
        paragraph_index=body.paragraph_index,
        highlight_text=body.highlight_text,
        target_type=body.target_type,
        bbox=bbox,
    )
    annotations = db.get_annotations(book_id, page_num)
    created = next((a for a in annotations if a["id"] == annotation_id), None)
    return created


class AnnotationUpdate(BaseModel):
    content: str


@app.put("/api/annotations/{annotation_id}")
async def update_annotation(annotation_id: int, body: AnnotationUpdate):
    anno = db.get_annotation(annotation_id)
    if not anno:
        raise HTTPException(404, "Annotation not found")
    if anno["author"] != "user":
        raise HTTPException(403, "只能编辑自己的批注")
    db.update_annotation(annotation_id, body.content)
    return db.get_annotation(annotation_id)


@app.delete("/api/annotations/{annotation_id}")
async def delete_annotation(annotation_id: int):
    anno = db.get_annotation(annotation_id)
    if not anno:
        raise HTTPException(404, "Annotation not found")
    db.delete_annotation(annotation_id)
    return {"message": "Deleted"}


# ----- 聊天 -----

@app.get("/api/books/{book_id}/chat")
async def get_all_chat(book_id: int):
    """获取整本书的所有聊天消息（跨页）"""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    return db.get_all_chat_messages(book_id)


@app.get("/api/books/{book_id}/page/{page_num}/chat")
async def get_chat_messages(book_id: int, page_num: int):
    """获取某页的聊天消息（保留兼容）"""
    return db.get_chat_messages(book_id, page_num)


@app.post("/api/books/{book_id}/page/{page_num}/chat")
async def send_chat_message(book_id: int, page_num: int, body: ChatMessageCreate):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    msg_id = db.add_chat_message(
        book_id=book_id,
        page_number=page_num,
        author="user",
        content=body.content,
        highlight_text=body.highlight_text,
    )
    messages = db.get_chat_messages(book_id, page_num)
    created = next((m for m in messages if m["id"] == msg_id), None)
    return created


# ----- 阅读会话 -----

@app.post("/api/books/{book_id}/sessions/start")
async def start_session(book_id: int):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    session_id = db.start_session(book_id)
    return {"session_id": session_id}


@app.post("/api/books/{book_id}/sessions/end")
async def end_session(book_id: int, body: SessionEnd):
    db.end_session(body.session_id, body.pages_read)
    return {"message": "OK"}


# ----- 导出 Markdown -----

@app.get("/api/books/{book_id}/export")
async def export_markdown(book_id: int):
    """导出整本书的阅读笔记为 Markdown"""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    lines = [
        f"# {book['title']}",
        "",
        f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"进度: {book['current_page']}/{book['total_pages']} 页  ",
        f"类型: {book['file_type'].upper()}",
        "",
        "---",
        "",
    ]

    for page_num in range(1, book["total_pages"] + 1):
        page = db.get_page(book_id, page_num)
        if not page:
            continue

        annotations = db.get_annotations(book_id, page_num)
        memory = db.get_memory(book_id, page_num)
        chat_msgs = db.get_chat_messages(book_id, page_num)

        # 跳过没有任何内容的页（无批注、无记忆、无聊天）
        has_content = annotations or memory or chat_msgs
        if not has_content and page_num > book["current_page"]:
            continue

        lines.append(f"## 第 {page_num} 页")
        lines.append("")

        # 记忆摘要（如有）
        if memory:
            lines.append(f"> **摘要**: {memory['summary']}")
            if memory.get("keywords"):
                lines.append(f"> **关键词**: {memory['keywords']}")
            lines.append("")

        # 批注
        if annotations:
            lines.append("### 批注")
            lines.append("")
            for a in annotations:
                author = "Claude" if a["author"] == "claude" else USER_NAME
                target = a.get("target_type", "paragraph")
                loc = ""
                if target == "region":
                    loc = " *(区域批注)*"
                elif a.get("paragraph_index") is not None:
                    loc = f" *(段落 {a['paragraph_index']})*"

                if a.get("highlight_text"):
                    lines.append(f"- **{author}**{loc}: 「{a['highlight_text']}」— {a['content']}")
                else:
                    lines.append(f"- **{author}**{loc}: {a['content']}")
            lines.append("")

        # 聊天（如有）
        if chat_msgs:
            lines.append("### 讨论")
            lines.append("")
            for msg in chat_msgs:
                author = "Claude" if msg["author"] == "claude" else USER_NAME
                lines.append(f"- **{author}**: {msg['content']}")
            lines.append("")

        lines.append("---")
        lines.append("")

    md_content = "\n".join(lines)
    safe_title = book["title"].replace("/", "_").replace("\\", "_")
    filename = f"{safe_title}_notes.md"

    return Response(
        content=md_content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ----- 写作模式 -----

@app.put("/api/books/{book_id}/mode")
async def set_book_mode(book_id: int, body: ModeUpdate):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if body.mode not in ("read", "write"):
        raise HTTPException(400, "mode 必须是 'read' 或 'write'")
    if body.mode == "write" and book["file_type"] != "txt":
        raise HTTPException(400, "写作模式仅支持 TXT 文件")
    db.set_book_mode(book_id, body.mode)
    return {"message": "OK", "mode": body.mode}


@app.get("/api/books/{book_id}/draft/{page_num}")
async def get_draft(book_id: int, page_num: int):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if book.get("mode") != "write":
        raise HTTPException(400, "书不在写作模式")
    draft = db.init_draft_from_page(book_id, page_num)
    if not draft:
        raise HTTPException(404, "Page not found")
    from parser import split_paragraphs
    paragraphs = split_paragraphs(draft["content"])
    return {
        "content": draft["content"],
        "paragraphs": paragraphs,
        "revision": draft["revision"],
        "updated_at": draft["updated_at"],
    }


@app.put("/api/books/{book_id}/draft/{page_num}")
async def save_draft(book_id: int, page_num: int, body: DraftUpdate):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    if book.get("mode") != "write":
        raise HTTPException(400, "书不在写作模式")
    result = db.upsert_draft_page(book_id, page_num, body.content, body.expected_revision)
    if not result["ok"]:
        raise HTTPException(409, f"revision 冲突：期望 {body.expected_revision}，当前 {result['current_revision']}")
    return {"message": "OK", "revision": result["revision"]}


@app.post("/api/books/{book_id}/draft/{page_num}/versions")
async def create_version(book_id: int, page_num: int, body: VersionCreate):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    draft = db.get_draft_page(book_id, page_num)
    if not draft:
        raise HTTPException(404, "Draft not found")
    vid = db.save_version(book_id, page_num, draft["content"], body.label)
    return {"id": vid, "message": "Version saved"}


@app.get("/api/books/{book_id}/draft/{page_num}/versions")
async def list_versions(book_id: int, page_num: int):
    return db.get_versions(book_id, page_num)


@app.post("/api/books/{book_id}/draft/{page_num}/restore/{version_id}")
async def restore_version(book_id: int, page_num: int, version_id: int):
    result = db.restore_version(book_id, page_num, version_id)
    if not result["ok"]:
        raise HTTPException(404, result.get("error", "Restore failed"))
    return {"message": "Version restored", "revision": result["draft"]["revision"]}


# ----- 向量索引 -----

@app.post("/api/books/{book_id}/index")
async def build_index(book_id: int):
    """构建或重建整本书的向量索引"""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    from embedding import index_book
    result = await index_book(db, book_id)
    return result


@app.get("/api/books/{book_id}/index/status")
async def index_status(book_id: int):
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")
    count = db.count_embeddings(book_id)
    return {"book_id": book_id, "chunks": count, "total_pages": book["total_pages"]}


# ----- 建议管理 -----

@app.get("/api/books/{book_id}/page/{page_num}/suggestions")
async def list_suggestions(book_id: int, page_num: int):
    return db.get_suggestions(book_id, page_num, status=None)


@app.post("/api/books/{book_id}/page/{page_num}/suggestions/{suggestion_id}/accept")
async def accept_suggestion(book_id: int, page_num: int, suggestion_id: int):
    result = db.accept_suggestion_atomic(suggestion_id)
    if not result["ok"]:
        status_code = 409 if "stale" in result["error"] or "mismatch" in result["error"] else 400
        raise HTTPException(status_code, result["error"])
    return {"message": "Suggestion accepted", "revision": result["revision"]}


@app.post("/api/books/{book_id}/page/{page_num}/suggestions/{suggestion_id}/reject")
async def reject_suggestion(book_id: int, page_num: int, suggestion_id: int):
    sug = db.get_suggestion(suggestion_id)
    if not sug:
        raise HTTPException(404, "Suggestion not found")
    if sug["status"] != "pending":
        raise HTTPException(400, f"Suggestion is {sug['status']}, not pending")
    db.resolve_suggestion(suggestion_id, "rejected")
    return {"message": "Suggestion rejected"}


# ----- SSE 事件流 -----

@app.get("/api/books/{book_id}/events")
async def book_events(book_id: int):
    """SSE 端点：推送批注、聊天、进度变更事件给前端"""
    book = db.get_book(book_id)
    if not book:
        raise HTTPException(404, "Book not found")

    async def event_stream():
        # 初始快照
        last_anno_sig = _anno_signature(book_id, book["current_page"])
        last_chat_count = len(db.get_all_chat_messages(book_id))
        last_page = book["current_page"]
        last_draft_rev = _draft_revision(book_id, book["current_page"])
        last_sug_sig = _suggestion_signature(book_id, book["current_page"])
        last_theme = db.get_setting("theme") or "dark"
        last_custom_theme = db.get_setting("custom_theme") or "{}"

        while True:
            await asyncio.sleep(1)
            try:
                current_book = db.get_book(book_id)
                if not current_book:
                    break

                current_page = current_book["current_page"]

                # 进度变更（Claude 翻页）
                if current_page != last_page:
                    yield _sse_event("progress", {"current_page": current_page})
                    last_page = current_page
                    last_anno_sig = _anno_signature(book_id, current_page)
                    last_draft_rev = _draft_revision(book_id, current_page)
                    last_sug_sig = _suggestion_signature(book_id, current_page)

                # 批注变更
                new_sig = _anno_signature(book_id, current_page)
                if new_sig != last_anno_sig:
                    annos = db.get_annotations(book_id, current_page)
                    yield _sse_event("annotations", {"page": current_page, "annotations": annos})
                    last_anno_sig = new_sig

                # 聊天变更
                all_chat = db.get_all_chat_messages(book_id)
                if len(all_chat) != last_chat_count:
                    yield _sse_event("chat", {"messages": all_chat})
                    last_chat_count = len(all_chat)

                # 主题变更
                current_theme = db.get_setting("theme") or "dark"
                if current_theme != last_theme:
                    yield _sse_event("theme", {"theme": current_theme})
                    last_theme = current_theme

                # 自定义配色变更
                current_custom = db.get_setting("custom_theme") or "{}"
                if current_custom != last_custom_theme:
                    yield _sse_event("custom_theme", {"colors": current_custom})
                    last_custom_theme = current_custom

                # 草稿变更（写作模式）
                new_draft_rev = _draft_revision(book_id, current_page)
                if new_draft_rev != last_draft_rev:
                    draft = db.get_draft_page(book_id, current_page)
                    if draft:
                        yield _sse_event("draft", {
                            "page": current_page,
                            "revision": draft["revision"],
                            "content": draft["content"],
                        })
                    last_draft_rev = new_draft_rev

                # 建议变更（写作模式）
                new_sug_sig = _suggestion_signature(book_id, current_page)
                if new_sug_sig != last_sug_sig:
                    sugs = db.get_suggestions(book_id, current_page, status=None)
                    yield _sse_event("suggestions", {
                        "page": current_page,
                        "suggestions": sugs,
                    })
                    last_sug_sig = new_sug_sig

            except Exception as e:
                logger.warning("SSE stream error: %s", e)
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _anno_signature(book_id: int, page_number: int) -> str:
    annos = db.get_annotations(book_id, page_number)
    return ",".join(f"{a['id']}:{len(a['content'])}" for a in annos)


def _draft_revision(book_id: int, page_number: int) -> int:
    draft = db.get_draft_page(book_id, page_number)
    return draft["revision"] if draft else -1


def _suggestion_signature(book_id: int, page_number: int) -> str:
    sugs = db.get_suggestions(book_id, page_number, status=None)
    return ",".join(f"{s['id']}:{s['status']}" for s in sugs)


# ----- 设置 -----

_ALLOWED_SETTINGS = {
    "theme", "summary_api_base", "summary_api_key", "summary_model",
    "auto_comment_on_turn",
    "embedding_api_base", "embedding_api_key", "embedding_model",
}


@app.get("/api/settings")
async def get_settings():
    """获取所有设置（DB 值合并 config.py 默认值）"""
    db_settings = db.get_all_settings()
    defaults = {
        "theme": "dark",
        "summary_api_base": SUMMARY_API_BASE,
        "summary_api_key": SUMMARY_API_KEY,
        "summary_model": SUMMARY_MODEL,
        "auto_comment_on_turn": "false",
        "user_name": USER_NAME,
        "embedding_api_base": EMBEDDING_API_BASE,
        "embedding_api_key": EMBEDDING_API_KEY,
        "embedding_model": EMBEDDING_MODEL,
    }
    merged = {**defaults, **db_settings}
    if merged.get("summary_api_key"):
        key = merged["summary_api_key"]
        if len(key) > 8:
            merged["summary_api_key_masked"] = key[:4] + "****" + key[-4:]
        else:
            merged["summary_api_key_masked"] = "****"
    else:
        merged["summary_api_key_masked"] = ""
    return merged


@app.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    """更新设置"""
    filtered = {}
    for k, v in body.settings.items():
        if k in _ALLOWED_SETTINGS:
            filtered[k] = v
        else:
            logger.warning("Ignoring unknown setting key: %s", k)

    if filtered:
        db.set_settings_bulk(filtered)
    return {"message": "OK", "updated": list(filtered.keys())}


# ----- 配置 -----

@app.get("/api/config")
async def get_config():
    theme = db.get_setting("theme") or "dark"
    custom_theme = db.get_setting("custom_theme") or "{}"
    return {"user_name": USER_NAME, "theme": theme, "custom_theme": custom_theme}


# ========== 前端静态文件 ==========

@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


# ========== 启动 ==========

@app.on_event("shutdown")
def shutdown():
    db.close()


if __name__ == "__main__":
    logger.info("Starting web server on %s:%d", HTTP_HOST, HTTP_PORT)
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")

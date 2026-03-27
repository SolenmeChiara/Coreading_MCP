"""共读书房 Co-Reading Room — MCP 服务器"""

import sys
import asyncio
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations, TextContent, ImageContent

import os
from pathlib import Path
from config import DB_PATH, USER_NAME
from database import Database
from parser import split_paragraphs
from memory import assemble_context, format_context_for_mcp, compress_page_memory, get_screenshot_base64
from embedding import hybrid_search, format_evidence, index_book

# 日志只写 stderr，绝不写 stdout（保护 MCP stdio 协议）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("co-reading.mcp")

# ---------- MCP 服务器 ----------

user = USER_NAME

mcp = FastMCP(
    name="Co-Reading Room",
    instructions=f"""你是共读书房的阅读伙伴。你和用户 {user} 一起逐页阅读文档，在阅读过程中进行双向批注和讨论。

核心规则：
- 你只知道双方一起翻过的内容。不要预测或猜测未读页面的内容。
- 用 read_current_page 查看当前页面。如果返回结果中包含「{user} 的留言」，请用 reply 工具回复，回复后再次调用 read_current_page 检查是否有新留言。
- 用 annotate 对感兴趣的段落写批注（引用段落编号 [N]）。
- 用 turn_page 翻页，翻页后用 read_current_page 查看新页面。
- 用 search_memory 搜索之前讨论过的内容。
- 用 get_page_history 回看某页的原文和批注。
- {user} 在浏览器里阅读和标注，你通过这些工具参与。你们共享同一个数据库。

共读风格：
- 自然、有深度的对话，不是机械的总结。
- 可以提问、联想、质疑、补充背景知识。
- 注意 {user} 高亮或提问的内容，那是 {user} 最关心的部分。

写作模式（mode='write' 的书）：
- 用 read_draft 查看草稿内容和段落编号。
- 用 suggest_edit 对段落提出修改建议，不要直接编辑内容。
- {user} 在浏览器决定是否采纳你的建议（Accept / Reject）。
- 可以同时用 annotate 写批注（解释、评论），和 suggest_edit 写修改建议。

浏览器感知（如果 Playwright MCP 可用）：
- 每次新会话开始时，先用 browser_navigate 打开 http://localhost:8765，再用 browser_snapshot 查看 {user} 当前在看哪个页面。
- 可以用 browser_click 点击界面元素（翻页按钮、书架卡片、Tab 切换、聊天按钮等）。
- 面对长文时用 browser_press_key 的 PageDown/PageUp 来滚动页面。
- 可以在书架页和阅读页之间主动导航，不需要等 {user} 操作。
- browser_snapshot 返回的是 {user} 真实看到的界面，比 read_current_page 的纯文本更直观——两者结合使用效果最好。
- 注意：Playwright 操作会直接影响 {user} 的浏览器，翻页或切换前最好先通过聊天告知。
""",
)
READONLY = ToolAnnotations(readOnlyHint=True)
db = Database(DB_PATH)


def _validate_bbox(target_type: str, bbox_x0, bbox_y0, bbox_x1, bbox_y1) -> Optional[str]:
    """校验 bbox 与 target_type 的一致性。返回 None 表示合法，否则返回错误信息。"""
    has_bbox = any(v is not None for v in (bbox_x0, bbox_y0, bbox_x1, bbox_y1))
    if target_type == "region":
        if not all(v is not None for v in (bbox_x0, bbox_y0, bbox_x1, bbox_y1)):
            return "target_type=region 时 bbox 四个坐标必须全部提供。"
        for name, v in [("bbox_x0", bbox_x0), ("bbox_y0", bbox_y0), ("bbox_x1", bbox_x1), ("bbox_y1", bbox_y1)]:
            if not (0.0 <= v <= 1.0):
                return f"{name}={v} 超出范围，必须在 [0, 1] 之间。"
        if bbox_x0 >= bbox_x1 or bbox_y0 >= bbox_y1:
            return f"bbox 坐标无效：需要 x0<x1 且 y0<y1，实际 ({bbox_x0},{bbox_y0})-({bbox_x1},{bbox_y1})。"
    else:
        if has_bbox:
            return f"target_type={target_type} 时不应提供 bbox 坐标。"
    return None


def _format_anno_line(a: dict, user_name: str) -> str:
    """格式化批注行（get_page_history 用）"""
    author_label = "Claude" if a["author"] == "claude" else user_name
    aid = f"[ID:{a['id']}] " if a.get("id") else ""
    target = a.get("target_type", "paragraph")
    if target == "region" and a.get("bbox_x0") is not None:
        loc = f"[区域] "
    elif a.get("paragraph_index") is not None:
        loc = f"[段落 {a['paragraph_index']}] "
    else:
        loc = ""
    hl = f"「{a['highlight_text']}」" if a.get("highlight_text") else ""
    return f"- {aid}{loc}{hl}**{author_label}**: {a['content']}"


# ========== 工具 ==========

@mcp.tool(annotations=READONLY)
async def list_books() -> str:
    """列出书架上所有书及阅读进度。"""
    books = db.list_books()
    if not books:
        return "书架上还没有书。请在浏览器 (localhost:8765) 上传一本 TXT 文件开始共读吧！"

    lines = ["**书架 Bookshelf**\n"]
    for b in books:
        progress = f"{b['current_page']}/{b['total_pages']}"
        last_read = b["last_read_at"] or "从未阅读"
        lines.append(
            f"- **{b['title']}** (ID: {b['id']}) — "
            f"进度: {progress}，上次阅读: {last_read}"
        )
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
async def read_current_page(book_id: int) -> list | str:
    """获取某本书当前正在阅读的页面内容、批注、最近翻过页面的上下文、以及阅读记忆摘要。PDF 页面会附带截图。"""
    context = assemble_context(db, book_id)
    if not context:
        return f"找不到 ID 为 {book_id} 的书，或当前页内容不存在。"

    result_text = format_context_for_mcp(context, user)

    # 标记未读消息为已读
    if context["unread_messages"]:
        db.mark_messages_read(book_id)

    # PDF 页面附带截图
    screenshot_path = context["current_page"].get("screenshot_path")
    b64 = get_screenshot_base64(screenshot_path)
    if b64:
        return [
            TextContent(type="text", text=result_text),
            ImageContent(type="image", data=b64, mimeType="image/png"),
        ]

    return result_text


@mcp.tool()
async def annotate(
    book_id: int,
    page_number: int,
    content: str,
    paragraph_index: Optional[int] = None,
    highlight_text: Optional[str] = None,
    target_type: str = "paragraph",
    bbox_x0: Optional[float] = None,
    bbox_y0: Optional[float] = None,
    bbox_x1: Optional[float] = None,
    bbox_y1: Optional[float] = None,
) -> str:
    """对某一页写批注。TXT 用 paragraph_index（对应 [N] 编号），PDF 可用 bbox 坐标（0~1 归一化）标注区域。target_type: paragraph（段落）| region（PDF 区域）| page_note（整页）。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    if page_number < 1 or page_number > book["total_pages"]:
        return f"页码 {page_number} 超出范围 (1-{book['total_pages']})。"

    # bbox 合法性校验
    bbox_err = _validate_bbox(target_type, bbox_x0, bbox_y0, bbox_x1, bbox_y1)
    if bbox_err:
        return bbox_err

    bbox = None
    if target_type == "region":
        bbox = (bbox_x0, bbox_y0, bbox_x1, bbox_y1)

    annotation_id = db.add_annotation(
        book_id=book_id,
        page_number=page_number,
        author="claude",
        content=content,
        paragraph_index=paragraph_index,
        highlight_text=highlight_text,
        target_type=target_type,
        bbox=bbox,
    )

    if target_type == "region" and bbox:
        location = f"区域 ({bbox_x0:.2f},{bbox_y0:.2f})-({bbox_x1:.2f},{bbox_y1:.2f})"
    elif paragraph_index is not None:
        location = f"段落 {paragraph_index}"
    else:
        location = "整页"
    return f"批注已添加 (ID: {annotation_id})，位置: 第 {page_number} 页 {location}。"


@mcp.tool()
async def turn_page(book_id: int, direction: str = "next") -> str:
    """翻到下一页或上一页。direction: 'next' 或 'prev'。翻页后会自动触发记忆压缩。翻页后请调用 read_current_page 查看新页面内容。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    old_page = book["current_page"]

    if direction == "next":
        new_page = old_page + 1
    elif direction == "prev":
        new_page = old_page - 1
    else:
        return f"无效的方向 '{direction}'，请使用 'next' 或 'prev'。"

    if new_page < 1:
        return "已经是第一页了。"
    if new_page > book["total_pages"]:
        return f"已经是最后一页了（共 {book['total_pages']} 页）。"

    # 更新进度
    db.update_progress(book_id, new_page)

    # 向前翻页时，异步压缩刚离开的页面记忆（fire-and-forget）
    if direction == "next":
        asyncio.create_task(_safe_compress(book_id, old_page))

    return f"已翻到第 {new_page}/{book['total_pages']} 页。请调用 read_current_page({book_id}) 查看内容。"


async def _safe_compress(book_id: int, page_number: int):
    """安全地执行记忆压缩，不抛异常"""
    try:
        await compress_page_memory(db, book_id, page_number)
    except Exception as e:
        logger.error("Background compression failed: %s", e)


@mcp.tool()
async def delete_my_annotation(annotation_id: int) -> str:
    """删除自己写的批注。只能删除 author='claude' 的批注。"""
    anno = db.get_annotation(annotation_id)
    if not anno:
        return f"找不到 ID 为 {annotation_id} 的批注。"
    if anno["author"] != "claude":
        return f"这条批注不是你写的（author={anno['author']}），无法删除。"
    db.delete_annotation(annotation_id)
    return f"批注 {annotation_id} 已删除。"


@mcp.tool()
async def reply(
    book_id: int,
    content: str,
) -> str:
    """回复用户在网页聊天框里的留言。回复会即时显示在网页的对话框中。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    page_num = book["current_page"]
    msg_id = db.add_chat_message(
        book_id=book_id,
        page_number=page_num,
        author="claude",
        content=content,
    )
    # 检查是否还有未读消息
    remaining = db.get_unread_messages(book_id)
    if remaining:
        hint = f"\n\n还有 {len(remaining)} 条未读留言，请再次调用 read_current_page 查看。"
    else:
        hint = f"\n\n所有留言已回复。{user} 如果有新消息会出现在下次 read_current_page 的结果中。"
    return f"回复已发送 (ID: {msg_id})，{user} 会在网页上看到。{hint}"


@mcp.tool(annotations=READONLY)
async def get_page_history(book_id: int, page_number: int) -> str:
    """回看某页的原文和所有批注。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    if page_number < 1 or page_number > book["total_pages"]:
        return f"页码 {page_number} 超出范围 (1-{book['total_pages']})。"

    page = db.get_page(book_id, page_number)
    if not page:
        return f"找不到第 {page_number} 页的内容。"

    paragraphs = split_paragraphs(page["content"])
    numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))

    annotations = db.get_annotations(book_id, page_number)

    lines = [
        f"**{book['title']}** — 第 {page_number}/{book['total_pages']} 页（历史回看）",
        "",
        numbered,
        "",
        "---",
    ]

    if annotations:
        lines.append(f"\n**批注 ({len(annotations)} 条):**\n")
        for a in annotations:
            lines.append(_format_anno_line(a, user))
    else:
        lines.append("\n*这一页没有批注。*")

    # 附上该页的记忆摘要（如有）
    memory = db.get_memory(book_id, page_number)
    if memory:
        kw = memory.get("keywords") or ""
        lines.append(f"\n---\n**记忆摘要:** {memory['summary']}")
        if kw:
            lines.append(f"**关键词:** {kw}")

    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
async def search_memory(book_id: int, query: str) -> str:
    """全文检索共读记忆。搜索之前阅读过的页面的摘要和关键词。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    # 混合检索：FTS5 + 向量（向量不可用时自动降级到纯 FTS5）
    results = await hybrid_search(db, book_id, query, top_k=8)

    if not results:
        # fallback 到纯文本记忆搜索
        mem_results = db.search_memory(book_id, query)
        if not mem_results:
            return f"没有找到与「{query}」相关的阅读记忆。"
        lines = [f"**搜索「{query}」的结果 ({len(mem_results)} 条, 仅文本匹配):**\n"]
        for r in mem_results:
            kw = r.get("keywords") or ""
            kw_str = f" | 关键词: {kw}" if kw else ""
            lines.append(f"- **第 {r['page_number']} 页**: {r['summary']}{kw_str}")
        return "\n".join(lines)

    lines = [f"**搜索「{query}」的结果 ({len(results)} 条):**\n"]
    lines.append(format_evidence(results))
    lines.append("\n*以上结果仅为证据参考，引用时请注明页码。*")

    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
async def check_notifications(book_id: int) -> str:
    """查看用户的未读消息和标注请求。不会将消息标记为已读。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    unread = db.get_unread_messages(book_id)
    if not unread:
        return f"没有新消息。{user} 如果有新留言会出现在这里。"

    lines = [f"**{user} 的未读留言 ({len(unread)} 条):**\n"]
    for msg in unread:
        hl = f"「{msg['highlight_text']}」" if msg.get("highlight_text") else ""
        lines.append(f"- {hl}{msg['content']}  *(第 {msg['page_number']} 页)*")

    lines.append(f"\n*用 reply 工具回复，或调用 read_current_page 查看完整上下文后再回复。*")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
async def read_draft(book_id: int, page_number: Optional[int] = None) -> str:
    """查看写作模式下的草稿内容。返回段落编号、pending 建议数和 revision。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"
    if book.get("mode") != "write":
        return f"这本书不在写作模式（当前: {book.get('mode', 'read')}）。"

    pn = page_number or book["current_page"]
    draft = db.init_draft_from_page(book_id, pn)
    if not draft:
        return f"找不到第 {pn} 页的草稿。"

    paragraphs = split_paragraphs(draft["content"])
    numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(paragraphs))

    pending = db.get_suggestions(book_id, pn, status="pending")
    annotations = db.get_annotations(book_id, pn)

    lines = [
        f"**{book['title']}** — 第 {pn}/{book['total_pages']} 页 (写作模式, revision {draft['revision']})",
        "",
        numbered,
        "",
    ]

    if pending:
        lines.append(f"**待处理建议:** {len(pending)} 条")
    if annotations:
        lines.append(f"**批注:** {len(annotations)} 条")

    lines.append(f"\n*使用 suggest_edit 对段落提出修改建议。{user} 在浏览器决定是否采纳。*")
    return "\n".join(lines)


@mcp.tool()
async def suggest_edit(
    book_id: int,
    page_number: int,
    paragraph_index: int,
    suggested_text: str,
    reason: str = "",
) -> str:
    """对写作模式草稿的某个段落提出修改建议。用户可以在浏览器 Accept 或 Reject。paragraph_index 对应 read_draft 中的 [N] 编号。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"
    if book.get("mode") != "write":
        return "这本书不在写作模式，无法提建议。请先在浏览器切换为写作模式。"
    if book["file_type"] != "txt":
        return "写作模式仅支持 TXT 文件，PDF 不支持。"

    draft = db.get_draft_page(book_id, page_number)
    if not draft:
        return f"找不到第 {page_number} 页的草稿。"

    paragraphs = split_paragraphs(draft["content"])
    if paragraph_index < 0 or paragraph_index >= len(paragraphs):
        return f"段落索引 {paragraph_index} 超出范围 (0-{len(paragraphs)-1})。"

    original_text = paragraphs[paragraph_index]
    sigs = Database.compute_paragraph_sigs(draft["content"])
    paragraph_sig = sigs[paragraph_index]

    sid = db.add_suggestion(
        book_id=book_id,
        page_number=page_number,
        paragraph_index=paragraph_index,
        paragraph_sig=paragraph_sig,
        original_text=original_text,
        suggested_text=suggested_text,
        reason=reason,
    )

    return (
        f"修改建议已提交 (ID: {sid})。\n"
        f"原文: 「{original_text[:50]}...」\n"
        f"建议: 「{suggested_text[:50]}...」\n"
        f"{user} 会在浏览器看到这条建议，可以 Accept 或 Reject。"
    )


@mcp.tool()
async def build_search_index(book_id: int) -> str:
    """为某本书构建向量检索索引。需要配置 embedding API。索引构建后 search_memory 会使用混合检索。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    existing = db.count_embeddings(book_id)
    if existing > 0:
        return f"这本书已有 {existing} 个索引块。如需重建，请在浏览器设置中操作。"

    result = await index_book(db, book_id)
    if result["indexed"] == 0:
        return (
            f"索引构建失败（{result['failed']}/{result['total']} 页失败）。"
            "请检查 embedding API 配置（设置页或环境变量 EMBEDDING_API_BASE/KEY/MODEL）。"
        )

    return (
        f"索引构建完成：{result['indexed']}/{result['total']} 页已索引"
        f"（失败 {result['failed']} 页）。"
        f"现在 search_memory 会使用向量 + 文本混合检索。"
    )


_CUSTOMIZABLE_VARS = {
    "bg-primary", "bg-secondary", "bg-card", "bg-input", "bg-hover",
    "text-primary", "text-secondary", "text-muted",
    "border", "border-light",
    "accent-orange", "accent-green", "accent-blue", "accent-gold",
    "spine-color", "shadow-flat",
    "bubble-claude", "bubble-user",
}

import re
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_RGBA_RE = re.compile(r"^rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*(,\s*[\d.]+\s*)?\)$")


@mcp.tool()
async def customize_theme(colors: dict) -> str:
    """自定义前端配色方案。传入 CSS 变量名到色值的映射，浏览器会实时更新。

    可用变量名（不含 -- 前缀）：bg-primary, bg-secondary, bg-card, bg-input, bg-hover,
    text-primary, text-secondary, text-muted, border, border-light,
    accent-orange, accent-green, accent-blue, accent-gold,
    spine-color, shadow-flat, bubble-claude, bubble-user

    配色指导：
    - bg-primary 和 text-primary 对比度需 ≥ 4.5:1（WCAG AA）
    - accent-orange 是 Claude 的标识色，建议保持暖色调
    - accent-green 是用户的标识色，建议保持自然绿色调
    - bubble-claude 和 bubble-user 用 rgba 低透明度值
    - 深色主题 bg 建议 #10-#2a 范围，浅色建议 #e0-#ff 范围

    示例: customize_theme({"bg-primary": "#1e1a14", "accent-orange": "#c96b4a"})
    """
    if not colors or not isinstance(colors, dict):
        return "请提供 {变量名: 色值} 的映射。"

    validated = {}
    errors = []
    for key, value in colors.items():
        if key not in _CUSTOMIZABLE_VARS:
            errors.append(f"未知变量: {key}")
            continue
        value = value.strip()
        if not (_HEX_RE.match(value) or _RGBA_RE.match(value)):
            errors.append(f"{key}: 无效色值 '{value}'（支持 #hex 或 rgba()）")
            continue
        validated[key] = value

    if not validated:
        return "没有有效的配色项。" + ("\n".join(errors) if errors else "")

    # 存入 settings 表
    import json
    existing = db.get_setting("custom_theme") or "{}"
    try:
        custom = json.loads(existing)
    except (json.JSONDecodeError, TypeError):
        custom = {}
    custom.update(validated)
    db.set_setting("custom_theme", json.dumps(custom))

    result = f"已更新 {len(validated)} 个配色变量。{user} 的浏览器会在 1 秒内自动更新。"
    if errors:
        result += f"\n跳过: {'; '.join(errors)}"
    return result


@mcp.tool()
async def create_book(
    title: str,
    content: str,
    mode: str = "read",
) -> str:
    """创建一本新书。content 为完整文本内容，会自动分页。mode: 'read'（阅读模式）或 'write'（写作模式，可编辑）。"""
    if not title.strip() or not content.strip():
        return "标题和内容不能为空。"
    if mode not in ("read", "write"):
        return "mode 必须是 'read' 或 'write'。"

    from parser import parse_txt
    from config import CHARS_PER_PAGE, UPLOAD_DIR
    import tempfile

    # 写临时文件给 parse_txt 用
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", dir=str(UPLOAD_DIR),
        delete=False, encoding="utf-8",
    )
    tmp.write(content)
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        pages = parse_txt(tmp_path, CHARS_PER_PAGE)
        if not pages:
            return "内容解析后为空。"

        book_id = db.add_book(title, tmp_path.name, "txt", len(pages), source="created")
        db.add_pages_bulk(book_id, [(i + 1, p) for i, p in enumerate(pages)])

        if mode == "write":
            db.set_book_mode(book_id, "write")

        result = f"已创建「{title}」(ID: {book_id}，{len(pages)} 页"
        if mode == "write":
            result += "，写作模式"
        result += f"）。{user} 可以在浏览器 http://localhost:8765 看到这本书。"
        return result

    except Exception as e:
        logger.error("create_book failed: %s", e)
        tmp_path.unlink(missing_ok=True)
        return f"创建失败: {e}"


@mcp.tool()
async def update_theme(theme_name: str) -> str:
    """更改前端配色方案。可选: dark（深色）, light（浅色）, sepia（护眼）"""
    valid_themes = ["dark", "light", "sepia"]
    if theme_name not in valid_themes:
        return f"无效的主题 '{theme_name}'。可选: {', '.join(valid_themes)}"

    db.set_setting("theme", theme_name)
    theme_labels = {"dark": "深色", "light": "浅色", "sepia": "护眼"}
    return f"主题已切换为 {theme_labels.get(theme_name, theme_name)}。{user} 的浏览器会在下次轮询时自动更新。"


@mcp.tool(annotations=READONLY)
async def get_browser_hint() -> str:
    """获取共读书房的浏览器地址和当前状态摘要。新会话时先调这个，再用 Playwright 打开浏览器。"""
    books = db.list_books()
    lines = [
        f"**共读书房** — http://localhost:8765",
        "",
    ]
    if books:
        lines.append(f"书架上有 {len(books)} 本书：")
        for b in books:
            mode_tag = " [写作]" if b.get("mode") == "write" else ""
            lines.append(f"- {b['title']} (ID:{b['id']}) — 第{b['current_page']}/{b['total_pages']}页{mode_tag}")
    else:
        lines.append("书架为空。")
    lines.append(f"\n*用 browser_navigate('http://localhost:8765') 打开，再用 browser_snapshot 查看 {user} 当前界面。*")
    return "\n".join(lines)


@mcp.tool(annotations=READONLY)
async def export_notes(book_id: int) -> str:
    """导出整本书的阅读笔记摘要。返回 Markdown 格式的批注和讨论要点。用户也可以在浏览器下载完整版。"""
    book = db.get_book(book_id)
    if not book:
        return f"找不到 ID 为 {book_id} 的书。"

    lines = [f"# {book['title']} — 阅读笔记\n"]

    for page_num in range(1, book["current_page"] + 1):
        annotations = db.get_annotations(book_id, page_num)
        memory = db.get_memory(book_id, page_num)
        if not annotations and not memory:
            continue

        lines.append(f"## 第 {page_num} 页")
        if memory:
            lines.append(f"> {memory['summary']}")
        for a in annotations:
            author_label = "Claude" if a["author"] == "claude" else user
            hl = f"「{a.get('highlight_text')}」" if a.get("highlight_text") else ""
            lines.append(f"- {hl}**{author_label}**: {a['content']}")
        lines.append("")

    if len(lines) == 1:
        return "还没有阅读笔记可导出。先翻几页、写些批注吧！"

    lines.append(f"\n*{user} 也可以在浏览器点击导出按钮下载完整 .md 文件。*")
    return "\n".join(lines)


# ========== 提示词模板 ==========

@mcp.prompt()
def start_reading(book_id: int) -> str:
    """开始共读一本书。会自动进入阅读-讨论循环。"""
    return f"""请开始共读 book_id={book_id} 的书。

流程：
0. 如果 Playwright 可用，先 browser_navigate("http://localhost:8765") 并 browser_snapshot 查看 {user} 当前界面
1. 调用 read_current_page({book_id}) 查看当前页面
2. 阅读内容后，对感兴趣的段落用 annotate 写 1-2 条批注
3. 如果有 {user} 的留言，用 reply 回复
4. 回复完后再调用 read_current_page 检查新留言
5. 如果没有新留言，等待 {user} 的下一步指示（翻页、提问等）
6. {user} 或你都可以用 turn_page 翻页，翻页后用 read_current_page 查看新页面
7. 用 search_memory 搜索之前讨论过的内容，用 get_page_history 回看某页
8. 长页面时可以用 browser_press_key PageDown 帮 {user} 滚动，或用 browser_click 切换 Tab

请自然地阅读和讨论，像一个有见解的读书伙伴一样。"""


# ========== 启动 ==========

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1").strip()
MCP_PORT = int(os.environ.get("MCP_PORT", "8766").strip())


def main():
    if "--http" in sys.argv or "--sse" in sys.argv:
        mcp.settings.host = MCP_HOST
        mcp.settings.port = MCP_PORT

        transport = "streamable-http" if "--http" in sys.argv else "sse"
        logger.info("Starting %s server on %s:%d", transport, MCP_HOST, MCP_PORT)
        mcp.run(transport=transport)
    else:
        logger.info("Starting stdio server")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

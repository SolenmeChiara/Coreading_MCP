"""共读书房 Co-Reading Room — 文档解析和分页"""

import logging
from pathlib import Path
from typing import Optional

from config import CHARS_PER_PAGE, SCREENSHOT_DPI

logger = logging.getLogger("co-reading.parser")

# 句末标点（单字符）
_SENTENCE_ENDINGS = set("。！？.!?")
# 句末标点后可能紧跟的闭合符号
_CLOSING_CHARS = set('"\'）」》】\u201d\u2019')


def _detect_encoding(file_path: Path) -> str:
    """检测文本编码：UTF-8 → GBK → latin-1"""
    raw = file_path.read_bytes()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def _find_cut_point(text: str, start: int, target_end: int) -> int:
    """
    在 text[start:target_end] 附近找最佳切分点。
    优先级：①段落边界 ②句末标点+闭合符号 ③单独句末标点 ④空白 ⑤硬切
    向回搜索范围：最多 500 字符。
    """
    search_floor = max(start, target_end - 500)

    # ① 段落边界（双换行）
    idx = text.rfind("\n\n", search_floor, target_end)
    if idx != -1:
        return idx + 2  # 切在双换行之后

    # ② / ③ 句末标点（可能带闭合符号）
    for i in range(target_end - 1, search_floor - 1, -1):
        if text[i] in _SENTENCE_ENDINGS:
            # 检查后面是否紧跟闭合符号，如果是就把闭合符号也包含进来
            cut = i + 1
            while cut < len(text) and text[cut] in _CLOSING_CHARS:
                cut += 1
            return cut

    # ④ 空白字符
    for i in range(target_end - 1, search_floor - 1, -1):
        if text[i] in (" ", "\n", "\t"):
            return i + 1

    # ⑤ 硬切
    return target_end


def parse_txt(file_path: Path, chars_per_page: int = CHARS_PER_PAGE) -> list[str]:
    """
    读取 TXT 文件，按字数分页，在段落/句子边界切分。
    返回页面内容字符串列表。
    """
    encoding = _detect_encoding(file_path)
    logger.info("Detected encoding for %s: %s", file_path.name, encoding)

    text = file_path.read_text(encoding=encoding, errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not text:
        return []

    pages: list[str] = []
    start = 0

    while start < len(text):
        end = start + chars_per_page

        if end >= len(text):
            page_text = text[start:].strip()
            if page_text:
                pages.append(page_text)
            break

        cut = _find_cut_point(text, start, end)
        page_text = text[start:cut].strip()
        if page_text:
            pages.append(page_text)

        # 跳过切分点处的空白
        start = cut
        while start < len(text) and text[start] in ("\n", " ", "\t"):
            start += 1

    logger.info("Parsed %s: %d pages (avg %d chars/page)",
                file_path.name, len(pages),
                sum(len(p) for p in pages) // max(len(pages), 1))
    return pages


def parse_pdf(file_path: Path, screenshot_dir: Path, book_id: int) -> list[dict]:
    """
    解析 PDF 文件。保留原始 PDF 页码，1 PDF 页 = 1 共读页。
    同时生成每页截图存入 screenshot_dir/{book_id}/page_N.png。
    提取结构化文本块（含 bbox 坐标）。

    返回: [{"page_number": 1, "content": "...", "screenshot_path": "...",
            "text_blocks": [...]}, ...]
    text_blocks 的 bbox 已归一化为 0~1，不需要绝对页面尺寸。
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(file_path))
    pages = []

    # 创建截图目录
    book_screenshot_dir = screenshot_dir / str(book_id)
    book_screenshot_dir.mkdir(parents=True, exist_ok=True)

    for i in range(doc.page_count):
        page = doc[i]
        page_number = i + 1

        # 提取文本
        text = page.get_text("text").strip()

        # 提取结构化文本块（含 bbox）
        text_blocks = _extract_text_blocks(page)

        # 生成截图
        screenshot_filename = f"page_{page_number}.png"
        screenshot_path = f"{book_id}/{screenshot_filename}"
        screenshot_full_path = book_screenshot_dir / screenshot_filename

        pix = page.get_pixmap(dpi=SCREENSHOT_DPI)
        pix.save(str(screenshot_full_path))

        pages.append({
            "page_number": page_number,
            "content": text,
            "screenshot_path": screenshot_path,
            "text_blocks": text_blocks,
        })

    doc.close()

    total_chars = sum(len(p["content"]) for p in pages)
    total_blocks = sum(len(p["text_blocks"]) for p in pages)
    logger.info("Parsed PDF %s: %d pages, %d chars, %d text blocks, screenshots in %s",
                file_path.name, len(pages), total_chars, total_blocks, book_screenshot_dir)
    return pages


def _extract_text_blocks(page) -> list[dict]:
    """
    从 PyMuPDF page 提取结构化文本块。
    bbox 归一化为 0~1 比例（相对于页面尺寸），方便前端适配任意渲染尺寸。
    """
    rect = page.rect
    w, h = rect.width, rect.height
    if w == 0 or h == 0:
        return []

    blocks = page.get_text("dict", flags=0)["blocks"]
    result = []
    idx = 0

    for block in blocks:
        if block["type"] != 0:  # 只要文本块，跳过图片块
            continue
        # 拼接 block 内所有 line 的文本
        lines = []
        for line in block.get("lines", []):
            spans_text = "".join(span["text"] for span in line.get("spans", []))
            if spans_text.strip():
                lines.append(spans_text)
        content = "\n".join(lines).strip()
        if not content:
            continue

        x0, y0, x1, y1 = block["bbox"]
        result.append({
            "block_index": idx,
            "content": content,
            "bbox": (x0 / w, y0 / h, x1 / w, y1 / h),  # 归一化
            "block_type": "text",
        })
        idx += 1

    return result


def split_paragraphs(content: str) -> list[str]:
    """
    将页面内容切分为段落列表。
    web.py 和 server.py 共用此函数，保证段落索引一致。
    """
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    return paragraphs

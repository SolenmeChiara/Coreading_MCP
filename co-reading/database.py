"""共读书房 Co-Reading Room — SQLite 数据库操作层"""

import sqlite3
import hashlib
import json
import threading
import time
import logging
from pathlib import Path
from typing import Optional

import shutil
from config import DB_PATH, UPLOAD_DIR, SCREENSHOT_DIR, SCHEMA_VERSION

logger = logging.getLogger("co-reading.database")

# ---------- 重试装饰器 ----------

def _retry_on_busy(func):
    """轻量重试：捕获 sqlite3.OperationalError，重试 3 次，间隔 0.1s"""
    def wrapper(*args, **kwargs):
        last_err = None
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    logger.warning("DB busy (attempt %d/3): %s", attempt + 1, e)
                    time.sleep(0.1)
                else:
                    raise
        raise last_err  # type: ignore[misc]
    return wrapper


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._init_schema()

    # ---------- 初始化 ----------

    def _configure_pragmas(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def _init_schema(self):
        with self._lock:
            # Phase 1 基础表
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL DEFAULT 'txt',
                    total_pages INTEGER NOT NULL,
                    current_page INTEGER DEFAULT 1,
                    last_read_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    screenshot_path TEXT,
                    FOREIGN KEY (book_id) REFERENCES books(id),
                    UNIQUE(book_id, page_number)
                );

                CREATE TABLE IF NOT EXISTS annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    paragraph_index INTEGER,
                    highlight_text TEXT,
                    author TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id)
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    author TEXT NOT NULL,
                    content TEXT NOT NULL,
                    highlight_text TEXT,
                    is_read BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id)
                );

                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Phase 2 新增表
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS reading_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    keywords TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id),
                    UNIQUE(book_id, page_number)
                );

                CREATE TABLE IF NOT EXISTS reading_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ended_at TIMESTAMP,
                    pages_read INTEGER DEFAULT 0,
                    FOREIGN KEY (book_id) REFERENCES books(id)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Phase 3 新增表：text_blocks（PDF 结构化文本块）
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS text_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    block_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    bbox_x0 REAL NOT NULL,
                    bbox_y0 REAL NOT NULL,
                    bbox_x1 REAL NOT NULL,
                    bbox_y1 REAL NOT NULL,
                    block_type TEXT NOT NULL DEFAULT 'text',
                    source TEXT NOT NULL DEFAULT 'native_pdf',
                    confidence REAL,
                    FOREIGN KEY (book_id) REFERENCES books(id)
                );

                CREATE INDEX IF NOT EXISTS idx_text_blocks_page
                    ON text_blocks(book_id, page_number);
            """)

            # Phase 3 迁移：annotations 表加 bbox 列（幂等）
            self._migrate_annotations_bbox()

            # Phase 4 新增表：写作模式
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS draft_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    paragraph_sigs TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id),
                    UNIQUE(book_id, page_number)
                );

                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    label TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id)
                );

                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    paragraph_index INTEGER NOT NULL,
                    paragraph_sig TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    suggested_text TEXT NOT NULL,
                    reason TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id)
                );
            """)

            # Phase 5 新增表：向量索引
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    page_number INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (book_id) REFERENCES books(id),
                    UNIQUE(book_id, page_number, chunk_index)
                );

                CREATE INDEX IF NOT EXISTS idx_embeddings_book_page
                    ON embeddings(book_id, page_number);
            """)

            # Phase 4 迁移：books 表加 mode 列（幂等）
            self._migrate_books_mode()

            # FTS5 全文检索索引（需要单独创建，不能放在 executescript 里）
            try:
                self.conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                        summary, keywords,
                        content='reading_memory',
                        content_rowid='id'
                    )
                """)
            except sqlite3.OperationalError as e:
                # FTS5 不可用时降级为普通 LIKE 搜索
                logger.warning("FTS5 not available, search will use LIKE fallback: %s", e)

            # FTS5 同步触发器
            self._create_fts_triggers()

            # 记录 schema 版本
            existing = self.conn.execute(
                "SELECT version FROM schema_version WHERE version = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
            if not existing:
                self.conn.execute(
                    "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            self.conn.commit()
            logger.info("Database schema initialized (version %d)", SCHEMA_VERSION)

    def _create_fts_triggers(self):
        """创建 FTS5 同步触发器，让 memory_fts 自动跟踪 reading_memory 的变更"""
        # 检查 memory_fts 表是否存在
        check = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        if not check:
            return

        self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS memory_fts_insert
            AFTER INSERT ON reading_memory BEGIN
                INSERT INTO memory_fts(rowid, summary, keywords)
                VALUES (new.id, new.summary, new.keywords);
            END;

            CREATE TRIGGER IF NOT EXISTS memory_fts_delete
            AFTER DELETE ON reading_memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, summary, keywords)
                VALUES ('delete', old.id, old.summary, old.keywords);
            END;

            CREATE TRIGGER IF NOT EXISTS memory_fts_update
            AFTER UPDATE ON reading_memory BEGIN
                INSERT INTO memory_fts(memory_fts, rowid, summary, keywords)
                VALUES ('delete', old.id, old.summary, old.keywords);
                INSERT INTO memory_fts(rowid, summary, keywords)
                VALUES (new.id, new.summary, new.keywords);
            END;
        """)

    def _migrate_annotations_bbox(self):
        """给 annotations 表加 bbox 列（幂等迁移）"""
        cursor = self.conn.execute("PRAGMA table_info(annotations)")
        columns = {row[1] for row in cursor.fetchall()}
        new_cols = [
            ("target_type", "TEXT NOT NULL DEFAULT 'paragraph'"),
            ("bbox_x0", "REAL"),
            ("bbox_y0", "REAL"),
            ("bbox_x1", "REAL"),
            ("bbox_y1", "REAL"),
        ]
        for col_name, col_def in new_cols:
            if col_name not in columns:
                self.conn.execute(f"ALTER TABLE annotations ADD COLUMN {col_name} {col_def}")
                logger.info("Added column annotations.%s", col_name)
        self.conn.commit()

    def _migrate_books_mode(self):
        """给 books 表加 mode 和 source 列（幂等迁移）"""
        cursor = self.conn.execute("PRAGMA table_info(books)")
        columns = {row[1] for row in cursor.fetchall()}
        changed = False
        if "mode" not in columns:
            self.conn.execute("ALTER TABLE books ADD COLUMN mode TEXT NOT NULL DEFAULT 'read'")
            logger.info("Added column books.mode")
            changed = True
        if "source" not in columns:
            self.conn.execute("ALTER TABLE books ADD COLUMN source TEXT NOT NULL DEFAULT 'upload'")
            logger.info("Added column books.source")
            changed = True
        if changed:
            self.conn.commit()

    @property
    def has_fts(self) -> bool:
        """检查 FTS5 是否可用"""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        return row is not None

    def close(self):
        with self._lock:
            self.conn.close()

    # ---------- 辅助 ----------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return dict(row)

    def _rows_to_dicts(self, rows: list[sqlite3.Row]) -> list[dict]:
        return [dict(r) for r in rows]

    # ---------- Books ----------

    @_retry_on_busy
    def add_book(self, title: str, filename: str, file_type: str, total_pages: int,
                 source: str = "upload") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO books (title, filename, file_type, total_pages, source) VALUES (?, ?, ?, ?, ?)",
                (title, filename, file_type, total_pages, source),
            )
            self.conn.commit()
            logger.info("Added book '%s' (id=%d, pages=%d)", title, cur.lastrowid, total_pages)
            return cur.lastrowid  # type: ignore[return-value]

    def list_books(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM books ORDER BY updated_at DESC"
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_book(self, book_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    @_retry_on_busy
    def delete_book_cascade(self, book_id: int) -> bool:
        with self._lock:
            book = self.conn.execute(
                "SELECT filename FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            if not book:
                return False

            # 删除数据库记录（包括 Phase 2/3/4/5 新增表）
            self.conn.execute("DELETE FROM embeddings WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM suggestions WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM versions WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM draft_pages WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM text_blocks WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM reading_memory WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM reading_sessions WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM chat_messages WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM annotations WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM pages WHERE book_id = ?", (book_id,))
            self.conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
            self.conn.commit()

            # 删除上传文件（路径穿越防护）
            filename = book["filename"]
            file_path = (UPLOAD_DIR / filename).resolve()
            if file_path.is_relative_to(UPLOAD_DIR.resolve()) and file_path.exists():
                file_path.unlink()
                logger.info("Deleted upload file: %s", file_path)
            else:
                logger.warning("Skipped file deletion (path check failed): %s", file_path)

            # 删除截图目录
            screenshot_book_dir = SCREENSHOT_DIR / str(book_id)
            if screenshot_book_dir.exists():
                shutil.rmtree(screenshot_book_dir, ignore_errors=True)
                logger.info("Deleted screenshot dir: %s", screenshot_book_dir)

            logger.info("Deleted book id=%d", book_id)
            return True

    # ---------- Pages ----------

    @_retry_on_busy
    def add_pages_bulk(self, book_id: int, pages: list[tuple]) -> None:
        """批量插入页面。pages: [(page_number, content), ...] 或 [(page_number, content, screenshot_path), ...]"""
        with self._lock:
            rows = []
            for p in pages:
                if len(p) >= 3:
                    rows.append((book_id, p[0], p[1], p[2]))
                else:
                    rows.append((book_id, p[0], p[1], None))
            self.conn.executemany(
                "INSERT INTO pages (book_id, page_number, content, screenshot_path) VALUES (?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()

    def get_page(self, book_id: int, page_number: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pages WHERE book_id = ? AND page_number = ?",
                (book_id, page_number),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    # ---------- Progress ----------

    @_retry_on_busy
    def update_progress(self, book_id: int, page_number: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE books SET current_page = ?, last_read_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (page_number, book_id),
            )
            self.conn.commit()

    # ---------- Annotations ----------

    def get_annotations(self, book_id: int, page_number: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM annotations WHERE book_id = ? AND page_number = ? "
                "ORDER BY created_at ASC",
                (book_id, page_number),
            ).fetchall()
            return self._rows_to_dicts(rows)

    @_retry_on_busy
    def add_annotation(
        self,
        book_id: int,
        page_number: int,
        author: str,
        content: str,
        paragraph_index: Optional[int] = None,
        highlight_text: Optional[str] = None,
        target_type: str = "paragraph",
        bbox: Optional[tuple] = None,
    ) -> int:
        """添加批注。bbox=(x0,y0,x1,y1) 用于 PDF 区域批注。"""
        bx0, by0, bx1, by1 = bbox if bbox else (None, None, None, None)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO annotations "
                "(book_id, page_number, paragraph_index, highlight_text, author, content, "
                "target_type, bbox_x0, bbox_y0, bbox_x1, bbox_y1) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (book_id, page_number, paragraph_index, highlight_text, author, content,
                 target_type, bx0, by0, bx1, by1),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    @_retry_on_busy
    def update_annotation(self, annotation_id: int, content: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE annotations SET content = ? WHERE id = ?",
                (content, annotation_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    @_retry_on_busy
    def delete_annotation(self, annotation_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM annotations WHERE id = ?",
                (annotation_id,),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_annotation(self, annotation_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    # ---------- Text Blocks ----------

    @_retry_on_busy
    def add_text_blocks_bulk(self, book_id: int, page_number: int, blocks: list[dict]) -> None:
        """批量插入页面的结构化文本块。blocks: [{"block_index", "content", "bbox", "block_type", "source"?, "confidence"?}]"""
        with self._lock:
            rows = []
            for b in blocks:
                x0, y0, x1, y1 = b["bbox"]
                rows.append((
                    book_id, page_number, b["block_index"], b["content"],
                    x0, y0, x1, y1, b.get("block_type", "text"),
                    b.get("source", "native_pdf"), b.get("confidence"),
                ))
            self.conn.executemany(
                "INSERT INTO text_blocks "
                "(book_id, page_number, block_index, content, bbox_x0, bbox_y0, bbox_x1, bbox_y1, "
                "block_type, source, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()

    def get_text_blocks(self, book_id: int, page_number: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM text_blocks WHERE book_id = ? AND page_number = ? "
                "ORDER BY block_index ASC",
                (book_id, page_number),
            ).fetchall()
            return self._rows_to_dicts(rows)

    # ---------- Chat Messages ----------

    @_retry_on_busy
    def add_chat_message(
        self,
        book_id: int,
        page_number: int,
        author: str,
        content: str,
        highlight_text: Optional[str] = None,
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO chat_messages "
                "(book_id, page_number, author, content, highlight_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (book_id, page_number, author, content, highlight_text),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_chat_messages(self, book_id: int, page_number: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM chat_messages "
                "WHERE book_id = ? AND page_number = ? ORDER BY created_at ASC",
                (book_id, page_number),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_all_chat_messages(self, book_id: int) -> list[dict]:
        """获取整本书的所有聊天消息（跨页）"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM chat_messages WHERE book_id = ? ORDER BY created_at ASC",
                (book_id,),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_unread_messages(self, book_id: int) -> list[dict]:
        """获取用户发给 Claude 的未读消息"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM chat_messages "
                "WHERE book_id = ? AND author = 'user' AND is_read = 0 "
                "ORDER BY created_at ASC",
                (book_id,),
            ).fetchall()
            return self._rows_to_dicts(rows)

    @_retry_on_busy
    def mark_messages_read(self, book_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE chat_messages SET is_read = 1 "
                "WHERE book_id = ? AND author = 'user' AND is_read = 0",
                (book_id,),
            )
            self.conn.commit()

    # ---------- Reading Memory (Phase 2) ----------

    @_retry_on_busy
    def upsert_memory(self, book_id: int, page_number: int, summary: str, keywords: str) -> None:
        """插入或更新某页的阅读记忆摘要"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO reading_memory (book_id, page_number, summary, keywords) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(book_id, page_number) DO UPDATE SET "
                "summary = excluded.summary, keywords = excluded.keywords",
                (book_id, page_number, summary, keywords),
            )
            self.conn.commit()
            logger.info("Upserted memory for book %d page %d", book_id, page_number)

    def get_memory(self, book_id: int, page_number: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reading_memory WHERE book_id = ? AND page_number = ?",
                (book_id, page_number),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_memories_range(self, book_id: int, page_start: int, page_end: int) -> list[dict]:
        """获取指定页码范围内的记忆摘要"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reading_memory "
                "WHERE book_id = ? AND page_number >= ? AND page_number <= ? "
                "ORDER BY page_number ASC",
                (book_id, page_start, page_end),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def search_memory(self, book_id: int, query: str) -> list[dict]:
        """全文检索共读记忆。优先 FTS5，不可用时降级 LIKE"""
        with self._lock:
            if self.has_fts:
                rows = self.conn.execute(
                    "SELECT rm.* FROM reading_memory rm "
                    "JOIN memory_fts ON rm.id = memory_fts.rowid "
                    "WHERE memory_fts MATCH ? AND rm.book_id = ? "
                    "ORDER BY rank",
                    (query, book_id),
                ).fetchall()
            else:
                # LIKE fallback
                like_q = f"%{query}%"
                rows = self.conn.execute(
                    "SELECT * FROM reading_memory "
                    "WHERE book_id = ? AND (summary LIKE ? OR keywords LIKE ?) "
                    "ORDER BY page_number ASC",
                    (book_id, like_q, like_q),
                ).fetchall()
            return self._rows_to_dicts(rows)

    # ---------- Reading Sessions (Phase 2) ----------

    @_retry_on_busy
    def start_session(self, book_id: int) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO reading_sessions (book_id) VALUES (?)",
                (book_id,),
            )
            self.conn.commit()
            logger.info("Started session %d for book %d", cur.lastrowid, book_id)
            return cur.lastrowid  # type: ignore[return-value]

    @_retry_on_busy
    def end_session(self, session_id: int, pages_read: int = 0) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE reading_sessions SET ended_at = CURRENT_TIMESTAMP, "
                "pages_read = ? WHERE id = ?",
                (pages_read, session_id),
            )
            self.conn.commit()

    def get_active_session(self, book_id: int) -> Optional[dict]:
        """获取当前未结束的阅读会话"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reading_sessions "
                "WHERE book_id = ? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (book_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_last_session(self, book_id: int) -> Optional[dict]:
        """获取最近一次已完成的阅读会话"""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reading_sessions "
                "WHERE book_id = ? AND ended_at IS NOT NULL "
                "ORDER BY ended_at DESC LIMIT 1",
                (book_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    # ---------- Settings (Phase 2) ----------

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    @_retry_on_busy
    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()

    def get_all_settings(self) -> dict:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: r["value"] for r in rows}

    @_retry_on_busy
    def set_settings_bulk(self, settings: dict) -> None:
        with self._lock:
            for key, value in settings.items():
                self.conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
            self.conn.commit()

    # ---------- Writing Mode (Phase 4) ----------

    @staticmethod
    def compute_paragraph_sigs(content: str) -> list[str]:
        """计算段落签名列表（md5 hash）。和 split_paragraphs 保持一致的切分方式。"""
        paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
        return [hashlib.md5(p.encode("utf-8")).hexdigest()[:12] for p in paragraphs]

    @_retry_on_busy
    def set_book_mode(self, book_id: int, mode: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE books SET mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (mode, book_id),
            )
            self.conn.commit()

    # ----- Draft Pages -----

    def get_draft_page(self, book_id: int, page_number: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM draft_pages WHERE book_id = ? AND page_number = ?",
                (book_id, page_number),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    @_retry_on_busy
    def upsert_draft_page(
        self, book_id: int, page_number: int, content: str, expected_revision: Optional[int] = None,
    ) -> dict:
        """保存草稿。带 expected_revision 时做乐观锁检查。
        返回 {"ok": True, "revision": int} 或 {"ok": False, "current_revision": int}。
        """
        sigs = json.dumps(self.compute_paragraph_sigs(content))
        with self._lock:
            existing = self.conn.execute(
                "SELECT revision FROM draft_pages WHERE book_id = ? AND page_number = ?",
                (book_id, page_number),
            ).fetchone()

            if existing:
                current_rev = existing["revision"]
                if expected_revision is not None and current_rev != expected_revision:
                    return {"ok": False, "current_revision": current_rev}
                new_rev = current_rev + 1
                self.conn.execute(
                    "UPDATE draft_pages SET content = ?, revision = ?, paragraph_sigs = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE book_id = ? AND page_number = ?",
                    (content, new_rev, sigs, book_id, page_number),
                )
            else:
                new_rev = 1
                self.conn.execute(
                    "INSERT INTO draft_pages (book_id, page_number, content, revision, paragraph_sigs) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (book_id, page_number, content, new_rev, sigs),
                )
            self.conn.commit()
            return {"ok": True, "revision": new_rev}

    @_retry_on_busy
    def init_draft_from_page(self, book_id: int, page_number: int) -> Optional[dict]:
        """首次进入写作模式时，从 pages 懒拷贝到 draft_pages。返回 draft dict。"""
        existing = self.get_draft_page(book_id, page_number)
        if existing:
            return existing
        page = self.get_page(book_id, page_number)
        if not page:
            return None
        sigs = json.dumps(self.compute_paragraph_sigs(page["content"]))
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO draft_pages (book_id, page_number, content, revision, paragraph_sigs) "
                "VALUES (?, ?, ?, 0, ?)",
                (book_id, page_number, page["content"], sigs),
            )
            self.conn.commit()
        return self.get_draft_page(book_id, page_number)

    # ----- Versions -----

    @_retry_on_busy
    def save_version(self, book_id: int, page_number: int, content: str,
                     label: Optional[str] = None, source: str = "manual") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO versions (book_id, page_number, content, label, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (book_id, page_number, content, label, source),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_versions(self, book_id: int, page_number: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, book_id, page_number, label, source, created_at "
                "FROM versions WHERE book_id = ? AND page_number = ? "
                "ORDER BY created_at DESC",
                (book_id, page_number),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def get_version(self, version_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    @_retry_on_busy
    def restore_version(self, book_id: int, page_number: int, version_id: int) -> dict:
        """恢复版本：先存当前草稿为 restore 版本，再覆盖草稿。返回新草稿。"""
        with self._lock:
            version = self.conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if not version:
                return {"ok": False, "error": "version not found"}

            # 存当前草稿为 restore 版本
            draft = self.conn.execute(
                "SELECT content FROM draft_pages WHERE book_id = ? AND page_number = ?",
                (book_id, page_number),
            ).fetchone()
            if draft:
                self.conn.execute(
                    "INSERT INTO versions (book_id, page_number, content, label, source) "
                    "VALUES (?, ?, ?, '恢复前自动保存', 'restore')",
                    (book_id, page_number, draft["content"]),
                )

            # 覆盖草稿
            new_content = version["content"]
            sigs = json.dumps(self.compute_paragraph_sigs(new_content))
            self.conn.execute(
                "UPDATE draft_pages SET content = ?, revision = revision + 1, "
                "paragraph_sigs = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE book_id = ? AND page_number = ?",
                (new_content, sigs, book_id, page_number),
            )
            self.conn.commit()

        return {"ok": True, "draft": self.get_draft_page(book_id, page_number)}

    # ----- Suggestions -----

    @_retry_on_busy
    def add_suggestion(
        self, book_id: int, page_number: int, paragraph_index: int,
        paragraph_sig: str, original_text: str, suggested_text: str,
        reason: str = "",
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO suggestions "
                "(book_id, page_number, paragraph_index, paragraph_sig, "
                "original_text, suggested_text, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (book_id, page_number, paragraph_index, paragraph_sig,
                 original_text, suggested_text, reason),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_suggestions(self, book_id: int, page_number: int,
                        status: Optional[str] = "pending") -> list[dict]:
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM suggestions "
                    "WHERE book_id = ? AND page_number = ? AND status = ? "
                    "ORDER BY created_at ASC",
                    (book_id, page_number, status),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM suggestions "
                    "WHERE book_id = ? AND page_number = ? "
                    "ORDER BY created_at ASC",
                    (book_id, page_number),
                ).fetchall()
            return self._rows_to_dicts(rows)

    def get_suggestion(self, suggestion_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    @_retry_on_busy
    def resolve_suggestion(self, suggestion_id: int, status: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE suggestions SET status = ?, resolved_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (status, suggestion_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    @_retry_on_busy
    def accept_suggestion_atomic(self, suggestion_id: int) -> dict:
        """原子性应用建议：校验 pending → 匹配原文 → 替换段落 → 存版本 → 标记 accepted。
        返回 {"ok": True, "revision": int} 或 {"ok": False, "error": str}。
        """
        with self._lock:
            sug = self.conn.execute(
                "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            if not sug:
                return {"ok": False, "error": "suggestion not found"}
            if sug["status"] != "pending":
                return {"ok": False, "error": f"suggestion is {sug['status']}, not pending"}

            draft = self.conn.execute(
                "SELECT * FROM draft_pages WHERE book_id = ? AND page_number = ?",
                (sug["book_id"], sug["page_number"]),
            ).fetchone()
            if not draft:
                return {"ok": False, "error": "draft not found"}

            # 按签名匹配段落
            current_sigs = json.loads(draft["paragraph_sigs"] or "[]")
            target_sig = sug["paragraph_sig"]

            try:
                idx = current_sigs.index(target_sig)
            except ValueError:
                # 签名不匹配 → 标记 stale
                self.conn.execute(
                    "UPDATE suggestions SET status = 'stale', resolved_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?", (suggestion_id,),
                )
                self.conn.commit()
                return {"ok": False, "error": "paragraph changed, suggestion marked stale"}

            # 替换段落
            paragraphs = [p.strip() for p in draft["content"].split("\n") if p.strip()]
            if idx >= len(paragraphs) or paragraphs[idx] != sug["original_text"]:
                self.conn.execute(
                    "UPDATE suggestions SET status = 'stale', resolved_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?", (suggestion_id,),
                )
                self.conn.commit()
                return {"ok": False, "error": "paragraph text mismatch, suggestion marked stale"}

            paragraphs[idx] = sug["suggested_text"]
            new_content = "\n\n".join(paragraphs)
            new_sigs = json.dumps(self.compute_paragraph_sigs(new_content))
            new_rev = draft["revision"] + 1

            # 存 accept 版本
            self.conn.execute(
                "INSERT INTO versions (book_id, page_number, content, label, source) "
                "VALUES (?, ?, ?, ?, 'accept')",
                (sug["book_id"], sug["page_number"], draft["content"],
                 f"接受建议 #{suggestion_id} 前"),
            )

            # 更新草稿
            self.conn.execute(
                "UPDATE draft_pages SET content = ?, revision = ?, paragraph_sigs = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE book_id = ? AND page_number = ?",
                (new_content, new_rev, new_sigs, sug["book_id"], sug["page_number"]),
            )

            # 标记 accepted
            self.conn.execute(
                "UPDATE suggestions SET status = 'accepted', resolved_at = CURRENT_TIMESTAMP "
                "WHERE id = ?", (suggestion_id,),
            )

            self.conn.commit()
            return {"ok": True, "revision": new_rev}

    # ---------- Embeddings (Phase 5 - RAG) ----------

    @_retry_on_busy
    def upsert_embeddings(self, book_id: int, page_number: int,
                          chunks: list[dict]) -> int:
        """批量写入/更新页面的 embedding chunks。
        chunks: [{"chunk_index": 0, "content": "...", "embedding": bytes}, ...]
        先删除旧 chunks 再插入，保证幂等。返回插入条数。
        """
        with self._lock:
            self.conn.execute(
                "DELETE FROM embeddings WHERE book_id = ? AND page_number = ?",
                (book_id, page_number),
            )
            rows = [(book_id, page_number, c["chunk_index"], c["content"], c["embedding"])
                    for c in chunks]
            self.conn.executemany(
                "INSERT INTO embeddings (book_id, page_number, chunk_index, content, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()
            return len(rows)

    def get_all_embeddings(self, book_id: int) -> list[dict]:
        """获取整本书的所有 embedding 记录"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, book_id, page_number, chunk_index, content, embedding "
                "FROM embeddings WHERE book_id = ? ORDER BY page_number, chunk_index",
                (book_id,),
            ).fetchall()
            return self._rows_to_dicts(rows)

    def has_embeddings(self, book_id: int, page_number: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM embeddings WHERE book_id = ? AND page_number = ? LIMIT 1",
                (book_id, page_number),
            ).fetchone()
            return row is not None

    def count_embeddings(self, book_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM embeddings WHERE book_id = ?",
                (book_id,),
            ).fetchone()
            return row["cnt"] if row else 0

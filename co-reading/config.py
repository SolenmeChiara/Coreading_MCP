"""共读书房 Co-Reading Room — 配置项"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "library.db"
UPLOAD_DIR = DATA_DIR / "uploads"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
LOG_DIR = BASE_DIR / "logs"
FRONTEND_DIR = BASE_DIR / "frontend"

# 分页
CHARS_PER_PAGE = 3000
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
SCREENSHOT_DPI = 150                # PDF 截图分辨率

# HTTP 服务
HTTP_HOST = os.environ.get("HTTP_HOST", "127.0.0.1")
HTTP_PORT = 8765

# 数据库版本
SCHEMA_VERSION = 5

# 用户名（显示在批注和聊天中）
USER_NAME = os.environ.get("COREADING_USER", "Reader")

# 上下文窗口
RECENT_PAGES_FULL = 3       # 最近几页保留完整文本+批注
SUMMARY_PAGES_RANGE = 20    # 摘要覆盖范围（第4~20页）

# 记忆压缩模型（环境变量 fallback，运行时优先读 DB settings 表）
SUMMARY_API_BASE = os.environ.get("SUMMARY_API_BASE", "https://api.openai.com/v1")
SUMMARY_API_KEY = os.environ.get("SUMMARY_API_KEY", "")
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")

# Embedding（RAG 向量检索）
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "http://localhost:11434/v1")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "ollama")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_CHUNK_SIZE = 400       # 中文段落级 chunk 目标字数
EMBEDDING_CHUNK_OVERLAP = 80     # chunk 重叠字数

SUMMARY_PROMPT = """请用 2-3 句话总结以下页面的内容和讨论要点。保留关键概念、论点和双方的重要观点。
同时提取 3-5 个关键词用于后续检索。

页面内容：
{page_content}

批注讨论：
{annotations}

请以 JSON 格式返回：{{"summary": "...", "keywords": ["...", "..."]}}"""

# 确保目录存在
for _d in [DATA_DIR, UPLOAD_DIR, SCREENSHOT_DIR, LOG_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# CLAUDE.md — 共读书房 (Co-Reading Room)

## 项目概述

基于 MCP 协议的共读系统。用户在浏览器里阅读文档，Claude 通过 MCP 工具参与阅读和批注。
核心理念：「一起一点点看完一本书」——Claude 只知道双方一起翻过的内容，没有预读、没有全书索引。

## 用户

- 称呼：Sol
- 环境：Windows 11 专业版
- 中文交流，偶尔夹英文
- Python 初学者，技术决策需详细解释
- GitHub 邮箱：ruyuey0722@gmail.com

## 架构

### 双进程架构（方案 B）

```
Claude Desktop ←stdio→ MCP Server (server.py, FastMCP)
                              ↕
                         SQLite (WAL 模式)
                              ↕
Browser ←HTTP/SSE→ Web Server (web.py, FastAPI)
```

- `server.py`：MCP 服务器，stdio 模式注册到 Claude Desktop
- `web.py`：FastAPI HTTP 服务器，端口 8765，服务前端和 API
- 两个进程通过 SQLite 文件通信，WAL 模式 + busy_timeout 保证并发安全
- `start.bat`：一键启动两个进程

### 为什么拆成两个进程

1. MCP stdio 对 stdout 有严格要求，混合 HTTP 日志会干扰协议
2. 可独立重启、独立调试
3. 开源后用户可按需只启动一个

## 技术栈

- **后端**：Python 3.11+, FastMCP, FastAPI, uvicorn
- **数据库**：SQLite (WAL 模式), aiosqlite
- **文档解析**：PyMuPDF (fitz) for PDF, 内置 open() for TXT
- **前端**：纯 HTML + CSS + 原生 JS，无构建工具
- **记忆压缩**：OpenAI 兼容 SDK（统一 base_url + api_key + model）
- **实时推送**：SSE (Server-Sent Events)

## 数据模型

### SQLite 表

```sql
-- 书架
CREATE TABLE books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,            -- 'pdf' 或 'txt'
    total_pages INTEGER NOT NULL,
    current_page INTEGER DEFAULT 1,
    last_read_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 页面内容
CREATE TABLE pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    content TEXT NOT NULL,
    screenshot_path TEXT,               -- PDF 页面截图路径
    FOREIGN KEY (book_id) REFERENCES books(id),
    UNIQUE(book_id, page_number)
);

-- 批注
CREATE TABLE annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    paragraph_index INTEGER,            -- null 表示整页评论
    highlight_text TEXT,                -- 高亮的具体文本片段（可选）
    author TEXT NOT NULL,               -- 'user' 或 'claude'
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- 共读记忆
CREATE TABLE reading_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    summary TEXT NOT NULL,
    keywords TEXT,                      -- 逗号分隔的关键词
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES books(id),
    UNIQUE(book_id, page_number)
);

-- 阅读会话
CREATE TABLE reading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    pages_read INTEGER DEFAULT 0,
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- 用户交互队列（Call Claude 功能）
CREATE TABLE interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    highlight_text TEXT,
    user_message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT 0,          -- Claude 是否已读
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- 全局配置
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 全文检索索引
CREATE VIRTUAL TABLE memory_fts USING fts5(
    summary, keywords,
    content='reading_memory',
    content_rowid='id'
);

-- 数据库版本
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## MCP 工具

### 核心工具
| 工具 | 功能 |
|---|---|
| `list_books` | 列出书架和阅读进度 |
| `read_current_page` | 获取当前页内容+上下文+时间信息 |
| `annotate` | 对段落/高亮文本写批注 |
| `turn_page` | 翻页，触发异步记忆压缩 |
| `get_page_history` | 回看某页原文+批注 |
| `search_memory` | FTS5 全文检索共读记忆 |
| `check_notifications` | 查看用户的高亮/提问/Call Claude 请求 |
| `update_theme` | 更改前端配色方案 |

### 上下文拼装规则（read_current_page 返回内容）
- 当前页：完整文本 + PDF 截图 (base64) + 所有批注
- 最近 3 页：完整文本 + 完整批注
- 第 4~20 页：仅摘要 + 关键词（来自 reading_memory）
- 20 页以前：不主动加载，可通过 search_memory 检索
- 时间上下文：距上次阅读时长、本次阅读时长

## HTTP API

### 文档管理
- `POST /api/upload` — 上传文档 (PDF/TXT)
- `GET /api/books` — 列出所有书
- `DELETE /api/books/{id}` — 删除书

### 阅读
- `GET /api/books/{id}/page/{num}` — 获取页面内容
- `POST /api/books/{id}/progress` — 更新进度 `{ page: int }`
- `POST /api/books/{id}/sessions/start` — 开始阅读会话
- `POST /api/books/{id}/sessions/end` — 结束阅读会话

### 批注
- `GET /api/books/{id}/page/{num}/annotations` — 获取批注
- `POST /api/books/{id}/page/{num}/annotations` — 添加批注

### 交互
- `POST /api/books/{id}/call-claude` — 发送高亮/问题给 Claude
- `GET /api/books/{id}/page/{num}/events` — SSE 事件流（新批注推送）

### 设置
- `GET /api/settings` — 获取所有设置
- `PUT /api/settings` — 更新设置（主题、模型配置等）

## 文档解析规则

### PDF
- 使用 PyMuPDF 提取文本和截图
- **保留原始 PDF 页码**，一页 PDF = 一页共读
- 截图 DPI 150，存入 `data/screenshots/{book_id}/`
- 截图以 base64 编码返回给 Claude

### TXT
- 按字数分页，默认 3000 字/页
- 在句子边界切分（句号、问号、感叹号），不切断单词
- 编码检测：UTF-8 → GBK fallback

## 记忆压缩

### 模型配置
统一使用 OpenAI 兼容格式：`base_url + api_key + model`
- 支持 Anthropic、OpenRouter、硅基流动、Ollama 等任意 OpenAI 兼容端点
- Anthropic 额外支持原生 SDK（独立配置选项）
- 前端设置页面可配置，存入 settings 表
- 环境变量 fallback：`SUMMARY_API_BASE`, `SUMMARY_API_KEY`, `SUMMARY_MODEL`

### 压缩 Prompt
```
请用 2-3 句话总结以下页面的内容和讨论要点。保留关键概念、论点和双方的重要观点。
同时提取 3-5 个关键词用于后续检索。

页面内容：
{page_content}

批注讨论：
{annotations}

请以 JSON 格式返回：{"summary": "...", "keywords": ["...", "..."]}
```

### 异步策略
- 翻页时异步触发，不阻塞翻页响应
- 快速连续翻页时去重：只为最终停留的页面立即生成摘要，中间页延迟处理
- 摘要生成失败时记录日志，不影响阅读

## 前端设计

### 视觉风格（Anthropic 主题，默认深色）
- 深色背景：`#141413`
- 浅色背景：`#faf9f5`
- 中灰：`#b0aea5`
- 浅灰：`#e8e6dc`
- 强调橙：`#d97757`（Claude 批注气泡）
- 强调蓝：`#6a9bcc`
- 强调绿：`#788c5d`（用户批注气泡）
- 金色标记线：`#f0ad4e`
- 标题字体：Poppins, Arial
- 正文字体：Lora, Georgia（衬线）
- 行高 1.8，段落间距宽松

### 主题系统
- 所有颜色用 CSS 变量
- 预置多套主题（深色/浅色/护眼等）
- 支持自定义 CSS 变量
- settings 表存储当前主题
- MCP 工具 `update_theme` 可远程改配色

### 页面结构
1. 书架页：书目卡片，显示进度和上次阅读时间
2. 阅读页：
   - 顶栏：书名、页码、翻页按钮、阅读时长
   - 主体：文本区，段落可点击/可高亮
   - 批注气泡：用户绿色、Claude 橙色，圆角带小三角
   - 有批注的段落左侧金色竖线
   - 底部：提问框 + 「发送给 Claude」按钮
3. 设置页：主题切换、模型配置、分页参数

### 交互
- 点击段落 → 展开批注输入框
- 选中文本 → 高亮标注 + 可附加提问
- 左右方向键或按钮翻页
- 翻页时自动保存进度
- 打开书时恢复上次位置
- SSE 实时接收 Claude 批注
- 「Claude 正在思考...」动画 + 超时提示（30 秒超时，显示重试按钮）
- 翻页触发 Claude 行为可配置：自动评论 / 等待用户触发

### 禁止事项
- 不使用 AI 生图作为背景或装饰
- 图标可以用 AI 生成，但不能有明显 AI 生图痕迹

## 文件结构

```
co-reading/
├── server.py              # MCP 服务器（FastMCP, stdio）
├── web.py                 # FastAPI HTTP 服务器
├── parser.py              # 文档解析（PDF/TXT → 分页）
├── memory.py              # 上下文管理和记忆压缩
├── database.py            # SQLite 数据库操作
├── config.py              # 配置项
├── requirements.txt
├── start.bat              # Windows 一键启动脚本
├── .env.example           # 环境变量模板（不含真实密钥）
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── data/
│   ├── library.db         # SQLite 数据库（gitignore）
│   ├── uploads/           # 上传文件（gitignore）
│   └── screenshots/       # PDF 截图（gitignore）
├── logs/                  # 运行日志（gitignore）
└── README.md
```

## 实现阶段

### Phase 1：核心循环
1. SQLite 初始化 + schema version
2. TXT 上传和解析（按字数分页）
3. 后端 API：上传、分页读取、批注读写
4. MCP 工具：list_books, read_current_page, annotate
5. 前端：书架页 + 阅读页 + 批注显示和输入
6. 验证：上传 TXT → 浏览器阅读 → 写批注 → Claude 通过 MCP 读到并回应

### Phase 2：记忆系统
1. 翻页 + 进度保存 + 阅读会话时间追踪
2. 记忆压缩（异步，可配置模型）
3. read_current_page 上下文拼装（当前页+近页+摘要+时间）
4. search_memory FTS5 全文检索
5. get_page_history 回看
6. check_notifications + Call Claude 功能
7. 前端设置页（模型配置、主题切换）

### Phase 3：PDF 支持
1. PDF 文本提取和原始页分页
2. PDF 截图生成
3. 截图 base64 返回给 Claude
4. 前端适配 PDF 显示

### Phase 4：体验优化
1. SSE 替代轮询
2. 前端动画和过渡
3. 批注编辑和删除
4. 导出 Markdown 笔记
5. 多书同时阅读
6. Streamable HTTP 传输支持（第三方 MCP runner）
7. RAG 向量检索（Ollama 本地 embedding 或云端）

## 日志

- 后端日志输出到 `logs/` 目录 + 控制台
- MCP 服务器日志输出到 stderr（不干扰 stdio 协议）
- 记录：API 调用、MCP 工具调用、摘要生成成功/失败、数据库错误
- 日志级别可配置（DEBUG/INFO/WARNING/ERROR）

## 安全

- API 密钥通过环境变量管理，不硬编码
- `.env` 文件在 `.gitignore` 中
- MCP 配置文件含明文 API key，不对外暴露
- `.claude/` 目录在 `.gitignore` 中
- 上传文件限制 50MB
- 仅监听 localhost，不暴露到公网

## 开源准备

- 目标：以健壮、可定制的状态开源
- 提供 `.env.example` 模板
- README 包含安装和使用说明
- 支持第三方 MCP runner（Phase 4）
- Git 推送使用 Sol 的 GitHub 账户，不使用 Claude 的凭据

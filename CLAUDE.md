# CLAUDE.md — 共读书房 (Co-Reading Room)

## 项目概述

基于 MCP 协议的共读系统。用户在浏览器里阅读文档，Claude 通过 MCP 工具参与阅读和批注。
核心理念：「一起一点点看完一本书」——Claude 只知道双方一起翻过的内容，没有预读、没有全书索引。
具体的“感觉”存放在计划.txt内。

## 用户

- 称呼：Sol
- 环境：Windows 11 专业版
- 中文交流，偶尔夹英文
- Python 初学者，技术决策需详细解释
- 你必须要在完成任务时自己进行review和技术验证！跑验证不必问询意见，直接做。
- 本手册（CLAUDE.md）需要持续更新，完成功能、发现问题、有新想法时直接写入，不必确认。
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
    target_type TEXT NOT NULL DEFAULT 'paragraph',  -- paragraph | region | page_note
    bbox_x0 REAL,                      -- 归一化坐标 0~1（region 类型）
    bbox_y0 REAL,
    bbox_x1 REAL,
    bbox_y1 REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- PDF 结构化文本块
CREATE TABLE text_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    page_number INTEGER NOT NULL,
    block_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    bbox_x0 REAL NOT NULL,             -- 归一化坐标 0~1
    bbox_y0 REAL NOT NULL,
    bbox_x1 REAL NOT NULL,
    bbox_y1 REAL NOT NULL,
    block_type TEXT NOT NULL DEFAULT 'text',
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
| 工具 | 功能 | Phase |
|---|---|---|
| `list_books` | 列出书架和阅读进度 | 1 |
| `read_current_page` | 获取当前页内容+上下文+近页+摘要+时间 | 1+2 |
| `annotate` | 对段落/高亮文本写批注 | 1 |
| `reply` | 回复用户在网页聊天框的留言 | 1 |
| `turn_page` | 翻页（Claude 可主动翻页），触发异步记忆压缩 | 2 |
| `get_page_history` | 回看某页原文+批注+记忆摘要 | 2 |
| `search_memory` | FTS5 全文检索共读记忆（fallback LIKE） | 2 |
| `check_notifications` | 查看用户的未读消息（不标记已读） | 2 |
| `update_theme` | 更改前端配色方案（dark/light/sepia） | 2 |
| `export_notes` | 导出整本书的阅读笔记摘要 | 4 |
| `read_draft` | 查看写作模式草稿内容+段落编号+revision | 5 |
| `suggest_edit` | 对草稿段落提出修改建议（用户 Accept/Reject） | 5 |
| `build_search_index` | 为某本书构建向量检索索引 | 5 |
| `create_book` | 创建新书（Claude 生成内容，可选读/写模式） | 5 |

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

### 聊天
- `GET /api/books/{id}/page/{num}/chat` — 获取聊天消息
- `POST /api/books/{id}/page/{num}/chat` — 发送消息给 Claude

### 进度轮询
- `GET /api/books/{id}/current-progress` — 轻量端点，前端 3s 轮询检测 Claude 翻页

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
├── server.py              # MCP 服务器（FastMCP, stdio），8 个工具
├── web.py                 # FastAPI HTTP 服务器，端口 8765
├── parser.py              # 文档解析（PDF/TXT → 分页）
├── memory.py              # 上下文拼装 + 记忆压缩（Phase 2 新增）
├── database.py            # SQLite 数据库操作，11 张表
├── embedding.py           # RAG 向量检索（provider 抽象 + 分块 + 混合检索 + 降级）
├── config.py              # 配置项 + 环境变量 fallback
├── requirements.txt
├── start.bat              # Windows 一键启动脚本（stdio 模式）
├── start_http.bat         # Windows 一键启动（Streamable HTTP 模式）
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

### Phase 1：核心循环 ✅ 已完成
1. SQLite 初始化 + schema version
2. TXT 上传和解析（按字数分页）
3. 后端 API：上传、分页读取、批注读写
4. MCP 工具：list_books, read_current_page, annotate
5. 前端：书架页 + 阅读页 + 批注显示和输入
6. 额外：聊天系统（chat_messages 表 + chat API + 前端聊天面板 + reply MCP 工具）

### Phase 2：记忆系统 ✅ 已完成
1. turn_page MCP 工具（Claude 可主动翻页，浏览器 3s 轮询自动同步）
2. 阅读会话追踪（sessions/start + end + 前端计时器显示）
3. 记忆压缩（翻页时异步调用 OpenAI 兼容 API，fire-and-forget）
4. read_current_page 完整上下文拼装（当前页+近3页全文+第4-20页摘要+时间+未读消息）
5. search_memory FTS5 全文检索（FTS5 不可用时 LIKE fallback）
6. get_page_history 回看 + check_notifications 查看未读
7. update_theme 远程改配色（dark/light/sepia）
8. 前端设置页（齿轮入口 → 主题选择 + 模型配置 → 保存到 DB settings 表）
9. 三套主题：深色（默认）、浅色、护眼（CSS 变量切换，body 过渡动画）

### Phase 3：PDF 支持 + 跨页聊天 ✅ 已完成
1. PDF 上传和解析（PyMuPDF，1 PDF 页 = 1 共读页）
2. PDF 截图生成（DPI 150，存 data/screenshots/{book_id}/page_N.png）
3. MCP read_current_page 返回 TextContent + ImageContent（Claude 可"看到"PDF 截图）
4. 前端 PDF 截图显示（截图为主视图，提取文字可折叠隐藏，PDF 批注为整页级别）
5. 聊天面板改为跨页连续对话（全书消息 + 页码分隔标签）
6. /api/screenshots 端点提供截图文件，路径穿越防护
7. delete_book_cascade 级联清理截图目录

### Phase 4：体验优化（按 Sol 的优先级排序）
1. ~~批注编辑和删除~~ ✅ 已完成
   - 用户：hover 气泡显示编辑/删除按钮，只能改自己的
   - Claude：`delete_my_annotation` MCP 工具，只能删 author=claude 的
   - PUT /api/annotations/{id} + DELETE /api/annotations/{id}
   - 轮询改用 ID+内容签名比较（修复了 length 比较漏检问题）
2. ~~SSE 替代轮询~~ ✅ 已完成
   - GET /api/books/{id}/events SSE 端点（1s 轮询检测批注/聊天/进度变更）
   - 前端 EventSource 替代 3 个 setInterval，断连自动回退轮询
3. ~~PDF bbox 批注系统~~ ✅ 已完成
   - annotations 表新增 target_type + bbox_x0/y0/x1/y1 列（幂等迁移）
   - text_blocks 表存储 PDF 结构化文本块（含归一化 bbox 坐标）
   - 前端 canvas overlay 支持鼠标/触摸拖拽框选区域
   - 框选后弹出输入框，自动匹配选中区域的文本块
   - 已有 region 批注在截图上显示可点击高亮标记
   - MCP annotate 工具支持 target_type + bbox 参数
   - Schema version 2 → 3
4. ~~导出 Markdown 笔记~~ ✅ 已完成
   - MCP `export_notes` 工具：返回阅读过页面的批注摘要
   - HTTP `GET /api/books/{id}/export`：生成完整 Markdown（摘要+批注+讨论）
   - 前端阅读页导出按钮，点击下载 .md 文件
5. ~~多书同时阅读（tab 切换）~~ ✅ 已完成
   - `state.openBooks` 存储多本书的独立会话状态
   - 前端 tab 栏：多本书同时打开，点击切换，SSE 按需开关
   - 关闭 tab 时结束会话并清理 SSE 连接
   - 从书架重复点开已打开的书 → 直接切 tab，不重建
6. ~~Streamable HTTP 传输~~ ✅ 已完成
   - server.py 支持 `--http`（Streamable HTTP）和 `--sse` 启动参数
   - 可配置 `MCP_HOST`/`MCP_PORT` 环境变量（默认 127.0.0.1:8766）
   - `start_http.bat`：一键启动 Web + MCP HTTP 双服务器
   - 第三方 MCP 客户端连接 `http://host:port/mcp`
7. ~~RAG 向量检索~~ ✅ 已完成
   - `embedding.py`：provider 抽象（Ollama/云端 OpenAI 兼容）、中文段落级分块（带重叠）
   - 混合检索：FTS5 + 向量双路召回 → 重排去重 → 可追溯证据格式
   - 全失败路径降级：embedding 不可用→纯 FTS5；FTS5 也失败→LIKE；全空→明确提示
   - `search_memory` MCP 工具增强为混合检索（向量索引存在时自动启用）
   - `build_search_index` MCP 工具触发索引构建
   - `POST /api/books/{id}/index` + `GET /api/books/{id}/index/status` HTTP 端点
   - 设置页支持 embedding API 配置（base_url/api_key/model）
   - Schema version 4→5，embeddings 表幂等迁移，cascade delete
8. 前端动画和过渡

### Phase 5：写作模式 ✅ 已完成
核心理念：Claude 作为写作搭档，可以批注也可以提出结构化修改建议，但永远不直接改正文。

1. **数据层**（Schema v3 → v4）
   - books 表加 `mode` 列（read/write）
   - `draft_pages` 表：可编辑草稿，带 revision 乐观锁 + paragraph_sigs JSON
   - `versions` 表：版本快照，带 source（manual/accept/restore）
   - `suggestions` 表：Claude 修改建议，段落签名锚定，状态流转 pending→accepted/rejected/stale
2. **MCP 工具**
   - `read_draft`：查看草稿内容 + 段落编号 + revision
   - `suggest_edit`：对段落提出修改建议（仅 TXT + write 模式）
3. **HTTP API**
   - `PUT /api/books/{id}/mode` 切换模式
   - `GET/PUT /api/books/{id}/draft/{page}` 草稿 CRUD（带 revision 乐观锁）
   - `POST /api/books/{id}/draft/{page}/versions` 保存版本
   - `POST /api/books/{id}/draft/{page}/restore/{vid}` 恢复版本（自动保存当前）
   - `GET /api/books/{id}/page/{num}/suggestions` 列出建议
   - `POST .../suggestions/{id}/accept|reject` 采纳/拒绝
   - SSE 新增 `draft` 和 `suggestions` 事件
4. **前端**
   - 模式切换按钮（仅 TXT 显示）
   - `renderWriteMode`：段落点击编辑、auto-save 1.5s debounce、revision 冲突自动刷新
   - 建议卡片：删除线旧文 + 高亮新文 + Accept/Reject
   - 版本面板：保存/恢复/历史列表
   - stale 建议自动检测和展示

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
- 仓库地址：https://github.com/SolenmeChiara/Coreading_MCP

## 欢迎文本

- `data/welcome.txt` 是新用户首次启动时自动导入的默认文本
- 内容是关于共读意义、批注意义、阅读交流意义的思考 + 项目介绍
- web.py 启动时检测书架为空则自动 seed，已有书时跳过
- 不使用第一人称

## 已知问题和注意事项

- **Schema 迁移**：database.py 用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` 实现幂等迁移，v1/v2 数据库升级 v3 时自动创建新表和列，不丢数据
- **记忆压缩依赖外部 API**：需要在设置页或环境变量配置 `SUMMARY_API_BASE/KEY/MODEL`，未配置时翻页正常但不生成摘要（静默跳过）
- ~~批注轮询用数量比较~~ 已修复：改用 ID+内容长度签名比较，编辑和删除都能正确检测
- **`@app.on_event("shutdown")`**：FastAPI 旧 API，有 deprecation warning，Phase 4 可改为 lifespan
- **FTS5 中文分词**：SQLite FTS5 默认按空格分词，对中文分词效果有限（整词匹配可以，拆字不行）。Phase 4 引入 RAG 时可解决

## PDF 批注架构决策

**问题**：PDF 提取文字（语义坐标系）和截图（视觉坐标系）冲突。OCR 不准时，段落定位失真，文字被打乱。

**已实现方案（Phase 4.3）**：
- 截图为主视图，提取文字折叠隐藏
- 三种批注类型：region（bbox 区域）| page_note（整页）| paragraph（TXT 段落）
- bbox 坐标归一化为 0~1（相对于页面尺寸），前端 canvas 渲染适配任意尺寸
- text_blocks 表存储 PDF 结构化文本块（含 bbox），框选时自动匹配文本
- 前端 canvas overlay 支持鼠标/触摸拖拽框选，弹出式输入框
- 已有 region 批注在截图上显示可点击高亮标记，点击跳转到对应气泡
- MCP annotate 工具支持 target_type + bbox 参数，Claude 可以标注特定区域
- TXT 模式不变，保留段落级批注

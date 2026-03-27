# 共读书房 Co-Reading Room

基于 MCP 协议的共读系统。用户在浏览器里阅读文档，Claude 通过 MCP 工具参与阅读和批注。

核心理念：**「一起一点点看完一本书」**——Claude 只知道双方一起翻过的内容，没有预读、没有全书索引。

## 架构

```
Claude Desktop ←stdio→ MCP Server (server.py, FastMCP)
                              ↕
                         SQLite (WAL 模式)
                              ↕
Browser ←HTTP/SSE→ Web Server (web.py, FastAPI)
```

双进程架构：MCP 服务器走 stdio 与 Claude 通信，Web 服务器走 HTTP 与浏览器通信，两者通过 SQLite 文件共享状态。

### 为什么拆两个进程

- MCP stdio 对 stdout 有严格要求，混合 HTTP 日志会干扰协议
- 可独立重启、独立调试
- 开源后用户可按需只启动一个

## 功能

### 阅读模式
- 上传 TXT / PDF 文档，自动分页
- 逐页阅读，Claude 参与批注和讨论
- PDF 截图为主视图 + canvas 区域框选批注（bbox 坐标锚定）
- 跨页聊天面板，SSE 实时推送
- 记忆压缩：翻页时异步生成摘要，构建阅读上下文窗口
- RAG 混合检索：FTS5 + 向量双路召回，embedding 不可用时自动降级

### 写作模式
- Claude 可通过 `create_book` 工具生成新文档
- 段落级可编辑，revision 乐观锁防冲突
- Claude 通过 `suggest_edit` 提出修改建议，用户 Accept / Reject
- 段落签名锚定防漂移，版本快照可回滚

### 多书同时阅读
- Tab 栏切换，每本书独立会话状态和 SSE 连接
- 书架双栏布局：上传 | 创作

### 浏览器感知
- 配合 Playwright MCP，Claude 可以看到浏览器界面、点击按钮、滚动页面
- 每次新会话可通过 `get_browser_hint` + `browser_snapshot` 了解用户当前状态

### 自定义
- 三套预设主题（深色 / 浅色 / 护眼）
- Claude 可通过 `customize_theme` 实时修改任意 CSS 变量
- 导出 Markdown 阅读笔记

## MCP 工具一览

| 工具 | 功能 |
|---|---|
| `list_books` | 列出书架和阅读进度 |
| `read_current_page` | 获取当前页内容 + 上下文 + 摘要 |
| `annotate` | 写批注（段落/区域/整页） |
| `turn_page` | 翻页，触发记忆压缩 |
| `reply` | 回复用户聊天消息 |
| `delete_my_annotation` | 删除自己的批注 |
| `get_page_history` | 回看某页原文和批注 |
| `search_memory` | 混合检索阅读记忆（FTS5 + 向量） |
| `check_notifications` | 查看未读消息 |
| `update_theme` | 切换预设主题 |
| `customize_theme` | 自定义 CSS 配色变量 |
| `export_notes` | 导出阅读笔记摘要 |
| `create_book` | 创建新书（可选写作模式） |
| `read_draft` | 查看写作模式草稿 |
| `suggest_edit` | 对草稿段落提出修改建议 |
| `build_search_index` | 构建向量检索索引 |
| `get_browser_hint` | 获取浏览器地址和当前状态 |

## 安装教程

### 1. 环境要求

| 工具 | 最低版本 | 说明 |
|---|---|---|
| Python | 3.11+ | 推荐 3.12 或 3.13 |
| pip | 最新 | Python 自带 |
| Node.js | 18+ | 仅 Playwright 浏览器感知需要（可选） |
| Claude Desktop | 最新 | MCP 客户端 |

### 2. 克隆仓库

```bash
git clone https://github.com/SolenmeChiara/Coreading_MCP.git
cd Coreading_MCP
```

### 3. 安装依赖

```bash
cd co-reading
pip install -r requirements.txt
```

依赖说明：
- `fastmcp` + `mcp`：MCP 协议核心
- `fastapi` + `uvicorn`：Web 服务器
- `PyMuPDF`：PDF 解析和截图
- `openai`：记忆压缩和 RAG 向量检索（OpenAI 兼容格式，支持 Ollama、硅基流动、OpenRouter 等）
- `aiosqlite`：异步 SQLite 操作

### 4. 配置（可选）

```bash
cp .env.example .env
```

编辑 `.env` 填入你的配置。**不配置也能正常阅读和批注**，只是记忆压缩和向量检索不会工作。

```env
# 用户名（显示在批注和聊天中）
COREADING_USER=你的名字

# 记忆压缩（翻页时自动生成摘要）
SUMMARY_API_BASE=https://api.openai.com/v1
SUMMARY_API_KEY=sk-xxx
SUMMARY_MODEL=gpt-4o-mini

# 向量检索（默认本地 Ollama，也可用云端）
EMBEDDING_API_BASE=http://localhost:11434/v1
EMBEDDING_API_KEY=ollama
EMBEDDING_MODEL=nomic-embed-text
```

也可以启动后在浏览器设置页 (齿轮图标) 里配置，效果相同。

### 5. 启动

#### 方式 A：一键启动（Windows，推荐）

双击 `start.bat`，会自动启动 Web 服务器并打开浏览器。

MCP 服务器由 Claude Desktop 自动管理（见下方配置）。

#### 方式 B：手动启动

```bash
# 终端 1：启动 Web 服务器
python web.py
# 浏览器打开 http://localhost:8765

# MCP 服务器不需要手动启动，Claude Desktop 会通过 stdio 自动拉起
```

#### 方式 C：远程模式（手机访问）

双击 `start_http.bat`，同时启动：
- Web 服务器 → `http://你的局域网IP:8765`（手机浏览器打开）
- MCP HTTP 服务器 → `http://你的局域网IP:8766/mcp`（手机 Claude App 连接）

### 6. 连接 Claude Desktop

编辑 Claude Desktop 配置文件：
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

在 `mcpServers` 中添加：

```json
{
  "mcpServers": {
    "co-reading": {
      "command": "python",
      "args": ["你的路径/co-reading/server.py"],
      "env": {
        "COREADING_USER": "你的名字"
      }
    }
  }
}
```

重启 Claude Desktop，在对话中输入：

> 请用 start_reading prompt 开始共读 book_id=1 的书

### 7. 手机访问（可选）

**阅读页面**（局域网直连）：
1. 电脑运行 `start_http.bat`
2. 手机浏览器打开 `http://你的局域网IP:8765`（需同一网络或 Tailscale）

**MCP 连接手机 Claude App**（需公网）：
- claude.ai 的 Custom Connector 需要公网可达的 URL，局域网 IP 不行
- 方案 A：用 Tailscale Funnel / ngrok / Cloudflare Tunnel 暴露本地端口
- 方案 B：部署到云 VM
- 部署后在 claude.ai → Settings → Add Custom Connector，URL 填公网地址 + `/mcp`

### 8. 启用浏览器感知（可选）

在 Claude Desktop 配置中额外添加 Playwright MCP：

```json
{
  "mcpServers": {
    "co-reading": { "..." : "..." },
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

需要 Node.js 环境。首次使用时 Playwright 会自动下载 Chromium。

启用后 Claude 可以看到浏览器界面、点击按钮、滚动页面，实现更深度的共读体验。

### 9. 使用流程

1. 浏览器打开 `http://localhost:8765`
2. 拖拽上传 TXT 或 PDF 文件
3. 点击书卡进入阅读
4. 在 Claude Desktop 中让 Claude 开始共读
5. 双方可以独立写批注、聊天讨论
6. Claude 的批注会通过 SSE 实时出现在浏览器中
7. 点击齿轮图标进入设置，可配置主题和 API

### 配置项速查

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `COREADING_USER` | 用户名 | Reader |
| `SUMMARY_API_BASE` | 记忆压缩 API | https://api.openai.com/v1 |
| `SUMMARY_API_KEY` | 记忆压缩 API Key | （空） |
| `SUMMARY_MODEL` | 记忆压缩模型 | gpt-4o-mini |
| `EMBEDDING_API_BASE` | 向量检索 API | http://localhost:11434/v1 |
| `EMBEDDING_API_KEY` | 向量检索 API Key | ollama |
| `EMBEDDING_MODEL` | Embedding 模型 | nomic-embed-text |
| `MCP_HOST` | MCP HTTP 监听地址 | 127.0.0.1 |
| `MCP_PORT` | MCP HTTP 端口 | 8766 |
| `HTTP_HOST` | Web 服务器监听地址 | 127.0.0.1 |

## 技术栈

- **后端**：Python, FastMCP, FastAPI, uvicorn, aiosqlite
- **数据库**：SQLite（WAL 模式），11 张表
- **文档解析**：PyMuPDF (PDF), 内置 (TXT)
- **前端**：纯 HTML + CSS + 原生 JS，无构建工具
- **实时推送**：SSE (Server-Sent Events)
- **向量检索**：OpenAI 兼容 Embedding API + 纯 Python 余弦相似度

## 文件结构

```
co-reading/
├── server.py          # MCP 服务器（FastMCP, stdio/HTTP/SSE）
├── web.py             # FastAPI HTTP 服务器
├── parser.py          # 文档解析（PDF/TXT → 分页）
├── memory.py          # 上下文拼装 + 记忆压缩
├── embedding.py       # RAG 向量检索
├── database.py        # SQLite 数据库（11 张表）
├── config.py          # 配置项
├── requirements.txt
├── start.bat          # Windows 一键启动（stdio）
├── start_http.bat     # Windows 一键启动（HTTP 远程）
├── .env.example       # 环境变量模板
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
└── data/
    ├── welcome.txt    # 首次启动种子文本
    ├── library.db     # SQLite 数据库（gitignore）
    ├── uploads/       # 上传文件（gitignore）
    └── screenshots/   # PDF 截图（gitignore）
```

## 许可

MIT

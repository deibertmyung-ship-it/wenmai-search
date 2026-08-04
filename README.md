# Wenmai Search · 文脉

> 自托管、可追溯的混合知识检索系统。

Wenmai Search 把一批文档变成可搜索、可引用、可供 Agent 调用的知识服务：从文件导入、
版本管理和结构化切分，到 dense + sparse 混合召回、重排、引用定位，再通过 Web、REST、
CLI 与 MCP 暴露统一结果。

它专注于“找准并给出证据”，不内置聊天或答案生成层。你可以直接把它作为检索产品使用，
也可以将它接到任意 RAG、Agent 或内部知识应用之前。

## 为什么叫「文脉」

项目的核心不只是向量相似度，而是保留文档的来路与上下文：每条结果都能追溯到文档版本、
章节路径、字符区间、页码和源地址。“文脉”既对应这种可追溯性，也呼应仓库附带的中文古籍
演示语料；检索引擎本身不限定语言或领域。

## 主要能力

- **可恢复的导入流水线**：上传只负责落盘和入队，worker 按状态机解析、切分、嵌入和索引；任务支持租约抢占、重试与失败恢复。
- **幂等版本管理**：文档、版本、chunk 与对象键由稳定内容标识派生；重复导入不会制造重复数据，更新与删除有明确索引事件。
- **可追溯结构化切分**：保留 `heading_path`、`char_start` / `char_end`、页码、版面坐标、解析器版本和 `source_uri`。
- **混合检索**：dense 与字符级 BM25 sparse 双路召回，经 RRF 融合并可选 rerank；支持来源、文档、类型、ACL 和章节过滤。
- **可解释调试**：可返回查询改写、双路原始排名、融合贡献、重排分数、实际过滤条件和各阶段耗时。
- **多种接入面**：FastAPI REST、只读 MCP 工具、Typer CLI，以及服务端渲染的 Flask Web 界面。
- **本地与服务端双配置**：默认单机零外部服务运行，也可切换 PostgreSQL、S3/MinIO 和独立 Qdrant。

## 架构

```text
文件 / 目录
    │
    ▼
登记与版本控制 ──▶ 异步任务队列 ──▶ 解析 ──▶ 结构化切分 ──▶ dense + sparse 索引
    │                                      │                         │
    ├── SQLite / PostgreSQL                └── 可验证引用元数据       └── Qdrant
    └── 本地对象存储 / S3                                             │
                                                                       ▼
浏览器 ──▶ kbweb ──HTTP──▶ kbsvc REST ──▶ rewrite → retrieve → RRF → rerank → cite
Agent  ────────────────────────────────▶ MCP search / fetch / list
CLI    ────────────────────────────────▶ ingest / search / worker / reembed
```

仓库保留两个稳定的技术包名：`kbsvc` 是检索服务，`kbweb` 是通过 HTTP 调用它的表现层。

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。默认 `local` profile 使用 SQLite、
本地文件系统和嵌入式 Qdrant，不需要 Docker、外部数据库或在线模型。

### 1. 安装并初始化后端

```bash
git clone https://github.com/deibertmyung-ship-it/wenmai-search.git
cd wenmai-search/knowledge-service

uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
uv run kbsvc init
```

### 2. 导入示例语料并检索

```bash
uv run kbsvc ingest ../book --source guji --patterns "*.txt,*.md"
uv run kbsvc search "贼克如何取用神" --top-k 5
```

`book/` 只是演示语料；也可以换成自己的 TXT、Markdown，或安装可选解析器后导入 PDF、
Office 文档和图片。

### 3. 启动 REST 与 Web

先在后端目录启动 API：

```bash
uv run kbsvc serve
```

REST 文档位于 <http://127.0.0.1:8077/docs>。另开终端启动 Web：

```bash
cd knowledge-web
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,prod]"
uv run flask --app wsgi run --port 5055
```

访问 <http://127.0.0.1:5055>，即可使用检索、阅读器、书库、导入和任务监控界面。

## MCP 接入

本地 stdio 服务：

```bash
cd knowledge-service
uv run kbsvc mcp
```

首版只暴露三个只读工具：

| 工具 | 用途 |
|---|---|
| `search_knowledge` | 使用 hybrid / dense / sparse 模式检索，并返回可验证引用 |
| `fetch_document_chunks` | 按顺序读取命中位置附近的原文 chunk |
| `list_sources` | 查看当前身份可访问的知识来源 |

完整客户端配置示例见 [`knowledge-service/deploy/mcp-clients.json`](knowledge-service/deploy/mcp-clients.json)，鉴权与远程 Streamable HTTP 配置见 [`knowledge-service/docs/04-runbook.md`](knowledge-service/docs/04-runbook.md)。

## 运行模式

| 组件 | `local`（默认） | `server` |
|---|---|---|
| 元数据 | SQLite | PostgreSQL |
| 原始文件 | 本地文件系统 | S3 / MinIO |
| 向量库 | 嵌入式 Qdrant | Qdrant 服务 |
| dense embedding | 确定性 hash（零下载） | FastEmbed 或 OpenAI-compatible endpoint |
| sparse retrieval | 字符 1-gram / 2-gram + BM25 | 同左 |
| 任务执行 | 元数据库队列 + 单 worker | 共享队列 + 可水平扩展 worker |

服务端 Docker Compose 模板位于 [`knowledge-service/deploy/`](knowledge-service/deploy/)。所有配置项及默认值见 [`knowledge-service/.env.example`](knowledge-service/.env.example) 与 [`knowledge-web/.env.example`](knowledge-web/.env.example)。

## 仓库结构

```text
.
├── knowledge-service/   # kbsvc：导入、索引、检索、REST、MCP、CLI
├── knowledge-web/       # kbweb：检索 UI、阅读器、书库与任务管理
├── book/                # 中文古籍演示语料
├── knowledge-retrieval-github-survey.md
│                        # 立项阶段的 GitHub 技术架构调研
└── run.bat              # Windows 本地启动与维护脚本
```

更深入的设计资料：

- [`knowledge-service/docs/01-architecture.md`](knowledge-service/docs/01-architecture.md)：后端分层、状态机与检索链路
- [`knowledge-service/docs/02-data-model.md`](knowledge-service/docs/02-data-model.md)：元数据表与 Qdrant payload
- [`knowledge-service/docs/03-api.md`](knowledge-service/docs/03-api.md)：REST 与 MCP 契约
- [`knowledge-service/docs/04-runbook.md`](knowledge-service/docs/04-runbook.md)：部署、备份、排障与重建索引
- [`knowledge-service/docs/05-performance.md`](knowledge-service/docs/05-performance.md)：性能测试与扩容边界
- [`knowledge-web/docs/01-frontend-design.md`](knowledge-web/docs/01-frontend-design.md)：Web 端信息架构与视觉设计
- [`knowledge-retrieval-github-survey.md`](knowledge-retrieval-github-survey.md)：技术选型调研及原始候选依据

## 开发与验证

后端：

```bash
cd knowledge-service
uv run pytest -q
uv run ruff check .
```

Web：

```bash
cd knowledge-web
uv run pytest -q
uv run ruff check .
```

Playwright E2E 测试需要本机 Chrome，并需显式启用：

```bash
cd knowledge-web
uv pip install --python .venv -e ".[e2e]"
uv run pytest -m e2e tests/e2e -q
```

## 语料与许可证

项目源码及原创文档采用 [MIT License](LICENSE)。`book/` 中的文本仅用于检索演示和研究，
不属于 MIT 授权范围；其来源和权利状态以原始作品及 [`book/README.md`](book/README.md) 的说明为准。

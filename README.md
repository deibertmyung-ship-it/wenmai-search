# Wenmai Search · 文脉

> 自托管、可追溯的混合知识检索系统。

Wenmai Search 把一批文档变成可搜索、可引用、可供 Agent 调用的知识服务：从文件导入、
版本管理和结构化切分，到 dense 向量 + BM25 倒排混合召回、重排、引用定位，再通过 Web、
REST、CLI 与 MCP 暴露统一结果。

它专注于“找准并给出证据”，不内置聊天或答案生成层。你可以直接把它作为检索产品使用，
也可以将它接到任意 RAG、Agent 或内部知识应用之前。

## 为什么叫「文脉」

项目的核心不只是向量相似度，而是保留文档的“来龙去脉”：每条结果都能追溯到文档版本、
章节路径、字符区间、页码和源地址。“文脉”既对应这种可追溯性，也呼应仓库附带的中文古籍
演示语料；检索引擎本身不限定语言或领域。

## 主要能力

- **可恢复的导入流水线**：上传只负责落盘和入队，worker 按状态机解析、切分、嵌入和索引；任务支持租约抢占、重试与失败恢复。
- **幂等版本管理**：文档、版本、chunk 与对象键由稳定内容标识派生；重复导入不会制造重复数据，更新与删除有明确索引事件。
- **可追溯结构化切分**：保留 `heading_path`、`char_start` / `char_end`、页码、版面坐标、解析器版本和 `source_uri`。
- **混合检索**：dense 向量（Qdrant）与字符级 BM25 倒排（Tantivy）双路召回，经 RRF 融合并可选 rerank；支持来源、文档、类型、ACL 和章节过滤。
- **繁简与旧字形折叠**：混排语料下 `阴阳` 与 `陰陽` 检索到同一批段落；折叠只作用于索引与嵌入，展示的原文与引文保持原字形。
- **可解释调试**：可返回查询改写、双路原始排名、融合贡献、重排分数、实际过滤条件和各阶段耗时。
- **多种接入面**：FastAPI REST、只读 MCP 工具、Typer CLI，以及服务端渲染的 Flask Web 界面。
- **本地与服务端双配置**：本地使用 SQLite、本地文件、嵌入式 Qdrant + Tantivy 与 API 内置 worker；服务端可切换 PostgreSQL、S3/MinIO、独立 Qdrant 并水平扩展 worker。

## 架构

```text
文件 / 目录
    │
    ▼
登记与版本控制 ──▶ 异步任务队列 ──▶ 解析 ──▶ 结构化切分 ──┬─▶ dense 向量 ─▶ Qdrant
    │                                      │            └─▶ 词法倒排 ──▶ Tantivy
    ├── SQLite / PostgreSQL                └── 可验证引用元数据       │
    └── 本地对象存储 / S3                                             │
                                                                       ▼
浏览器 ──▶ kbweb ──HTTP──▶ kbsvc REST ──▶ rewrite → retrieve → RRF → rerank → cite
Agent  ────────────────────────────────▶ MCP search / fetch / list
CLI    ────────────────────────────────▶ ingest / search / worker / reembed
```

仓库保留两个稳定的技术包名：`kbsvc` 是检索服务，`kbweb` 是通过 HTTP 调用它的表现层。

## 快速开始

只需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)，本地模式不需要 Docker。
默认 `local` profile 将 SQLite、原始文件、嵌入式 Qdrant 与 Tantivy 索引都保存在 `.kbdata/`。

### 1. 安装后端与 Web

```bash
git clone https://github.com/deibertmyung-ship-it/wenmai-search.git
cd wenmai-search/knowledge-service

uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,fastembed]"

cd ../knowledge-web
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,prod]"
```

### 2. 一键启动本地服务

从仓库根目录执行：

```bat
run.bat
```

API 进程会同时持有嵌入式 Qdrant、Tantivy 索引和常驻 worker，脚本随后启动 Web。首次运行会
自动初始化 SQLite、Qdrant 集合与词法索引。访问 <http://127.0.0.1:5055> 后，上传任务会自动处理。

### 3. 使用 CLI 导入示例语料（可选）

嵌入式索引不能跨进程共享（Qdrant 与 Tantivy 都持目录锁）。先停止 API，再运行本地 CLI：

```bash
cd ..
run.bat stop
cd knowledge-service
uv run kbsvc ingest ../book --source guji --patterns "*.txt,*.md"
uv run kbsvc search "贼克如何取用神" --top-k 5
```

完成后回到仓库根目录重新执行 `run.bat`。日常导入建议直接使用 Web 页面，它会交给 API
内置 worker 自动处理。

`book/` 只是演示语料；也可以换成自己的 TXT、Markdown、PDF、Office 文档和图片。
Docling、Unstructured 与 Marker 已作为后端默认依赖安装，无需另装解析器。

REST 文档位于 <http://127.0.0.1:8077/docs>。停止、查看状态或重启整个本地栈：

```bat
run.bat stop
run.bat status
run.bat restart
```

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
| 向量库（dense） | API 进程内嵌 Qdrant（本地目录） | Qdrant Server |
| 词法索引（sparse） | 嵌入式 Tantivy（本地目录） | 同左 —— Tantivy 无服务端形态 |
| dense embedding | FastEmbed（默认 bge-small-zh-v1.5） | FastEmbed 或 OpenAI-compatible endpoint |
| sparse retrieval | 字符 1-gram / 2-gram 分词 + Tantivy BM25 | 同左 |
| 任务执行 | API 内置单 worker 线程，上传后自动处理 | 独立常驻 worker 容器（当前上限 1 个进程，见下） |

服务端 Docker Compose 模板位于 [`knowledge-service/deploy/`](knowledge-service/deploy/)，包含
postgres、qdrant、minio、api、worker、mcp、web 七个服务。所有配置项及默认值见
[`knowledge-service/.env.example`](knowledge-service/.env.example) 与
[`knowledge-web/.env.example`](knowledge-web/.env.example)。

两条 server profile 的硬约束：**worker 只能跑一个进程**（Tantivy 持目录独占锁，而 worker
内联写词法索引），且 **api / worker / mcp 必须共享同一个索引卷**（否则各写各的空索引，
`mode=sparse` 恒返回空且不报错）。详见
[`knowledge-service/docs/04-runbook.md`](knowledge-service/docs/04-runbook.md) 第 2 节。

## 仓库结构

```text
.
├── knowledge-service/   # kbsvc：导入、索引、检索、REST、MCP、CLI
├── knowledge-web/       # kbweb：检索 UI、阅读器、书库与任务管理
├── QA/                  # 项目问答与改造结论（HTML）
├── book/                # 中文古籍演示语料
├── knowledge-retrieval-github-survey.md
│                        # 立项阶段的 GitHub 技术架构调研
└── run.bat              # Windows 本地启动与维护脚本
```

更深入的设计资料：

- [`knowledge-service/docs/01-architecture.md`](knowledge-service/docs/01-architecture.md)：后端分层、状态机与检索链路
- [`knowledge-service/docs/02-data-model.md`](knowledge-service/docs/02-data-model.md)：元数据表、Qdrant payload 与 Tantivy 文档
- [`knowledge-service/docs/03-api.md`](knowledge-service/docs/03-api.md)：REST 与 MCP 契约
- [`knowledge-service/docs/04-runbook.md`](knowledge-service/docs/04-runbook.md)：部署、备份、排障与重建索引
- [`knowledge-service/docs/05-performance.md`](knowledge-service/docs/05-performance.md)：性能测试与扩容边界
- [`knowledge-web/docs/01-frontend-design.md`](knowledge-web/docs/01-frontend-design.md)：Web 端信息架构与视觉设计
- [`QA/2026-08-04-QA.html`](QA/2026-08-04-QA.html)：本次项目问答、故障分析与架构改造结论
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

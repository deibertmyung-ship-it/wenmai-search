# Knowledge Retrieval Survey

一个可自托管的知识检索系统原型，包含异步文档导入、可追溯切分、稠密与稀疏混合检索、REST/MCP 接口，以及配套的 Flask Web 界面。

项目从 GitHub 技术架构调研出发，落地为两个可独立运行的 Python 服务，并附带用于本地检索演示的中文古籍语料。

## 仓库结构

| 路径 | 内容 |
|---|---|
| [`knowledge-service/`](knowledge-service/) | `kbsvc` 检索后端：导入、切分、索引、混合检索、REST 与 MCP |
| [`knowledge-web/`](knowledge-web/) | `kbweb` Flask 前端：检索、阅读器、书库、导入与任务监控 |
| [`book/`](book/) | 中文传统术数古籍示例语料 |
| [`knowledge-retrieval-github-survey.md`](knowledge-retrieval-github-survey.md) | GitHub 技术架构调研报告 |
| [`knowledge-retrieval-github-survey.html`](knowledge-retrieval-github-survey.html) | 调研报告 HTML 版 |
| [`knowledge-retrieval-github-survey.xlsx`](knowledge-retrieval-github-survey.xlsx) | 调研结果表格 |

## 核心能力

- 本地模式零外部服务启动：SQLite、本地对象存储、嵌入式 Qdrant
- 可切换 PostgreSQL、S3/MinIO 与独立 Qdrant 的服务端部署模式
- 幂等文档导入及可重试的异步任务状态机
- 字符级 BM25 稀疏检索、可插拔稠密向量与 RRF 融合
- 稳定引用信息：文档、版本、标题路径、字符偏移与页码
- 面向 Agent 的最小只读 MCP 工具集
- 服务端渲染、无前端构建步骤的 Web 管理与检索界面

## 快速开始

需要 Python 3.11 和 [uv](https://docs.astral.sh/uv/)。先启动检索后端：

```bash
cd knowledge-service
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"

export KB_DATA_DIR="$PWD/.kbdata"
python -m kbsvc.cli init
python -m kbsvc.cli ingest ../book --source guji --patterns "*.txt,*.md"
python -m kbsvc.cli serve
```

后端 REST 文档默认位于 <http://127.0.0.1:8077/docs>。另开一个终端启动 Web 界面：

```bash
cd knowledge-web
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,prod]"
cp .env.example .env
flask --app wsgi run --port 5055
```

访问 <http://127.0.0.1:5055>。Windows PowerShell 的等价环境变量写法是：

```powershell
$env:KB_DATA_DIR = "$PWD/.kbdata"
```

更完整的配置、Docker 部署、API 契约与故障处理说明见 [`knowledge-service/README.md`](knowledge-service/README.md) 和 [`knowledge-service/docs/`](knowledge-service/docs/)。

## 验证

```bash
cd knowledge-service
python -m pytest -q
python -m ruff check .

cd ../knowledge-web
python -m pytest -q
python -m ruff check .
```

Web 端端到端测试需本机安装 Chrome，并按 [`knowledge-web/README.md`](knowledge-web/README.md) 中的说明显式启用。

## 语料说明

`book/` 中的文本用于检索演示与研究。语料来源及公共领域声明见 [`book/README.md`](book/README.md)。

## 许可证

项目源码及原创文档采用 [MIT License](LICENSE)。`book/` 中的语料不属于 MIT 授权范围，
其权利状态以原始作品及 [`book/README.md`](book/README.md) 中的说明为准。

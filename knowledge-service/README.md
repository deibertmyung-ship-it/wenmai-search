# kbsvc

自托管知识检索系统 —— 多格式文档批量导入、可追溯切分、混合检索，通过 **REST** 与 **MCP** 暴露统一查询能力。

```
文件 ──▶ 导入控制面 ──▶ 解析(Docling/可插拔) ──▶ 结构化切分 ──┬─▶ 稠密向量 ──▶ sqlite-vec / pgvector
                │                                          └─▶ 词法倒排 ──▶ fts5 / pg_search
                └── source/document/version/job 状态机、重试、索引事件                 │
                                                                                     ▼
                                          REST /v1/search  ◀── 检索管线 ──▶  MCP search_knowledge
                                                                (rewrite→fuse→rerank→citation)
```

## 本地默认架构

默认 `local` profile 无外部基础设施依赖，业务数据全部保存在本地：

| 组件 | local（默认） | server |
|---|---|---|
| 元数据 | SQLite | PostgreSQL |
| 对象存储 | 本地文件系统 | S3 / MinIO |
| 向量库（稠密） | sqlite-vec（同一 kbsvc.db） | pgvector |
| 词法索引 | fts5（同一 kbsvc.db） | pg_search |
| 队列 | SQLite + 租约 + API 内置 worker 线程 | PostgreSQL + 租约，可水平扩 worker |

根目录 `run.bat` 启动 API 与 Web；API 生命周期负责启动和停止内置 worker，并与请求线程
共享同一个 SQLite 文件。完整 server profile 仍使用 `deploy/docker-compose.yml`。

## 60 秒上手

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,fastembed]"

cd ..
run.bat
```

`run.bat` 会自动初始化数据库并在 API 内启动常驻 worker，随后可通过 Web 上传文档。若要在
`knowledge-service` 目录运行 `python -m kbsvc.cli ingest ...`，请先停止 API，避免第二个进程
与内置 worker 争用同一 SQLite 写锁。

```
[1] 六壬存验-清-吴师青 › 一、断例
    score=0.015625 rerank=0.81147
    …比用者，因上克下，有多下贼上，不成重审，又不成元首，贼克纷纷，难以取用，则寻比…
    cite: 5f26adf6-33c6-5e13-9cad-e67f51846400#25
```

起服务：
```bash
python -m kbsvc.cli serve       # REST  → http://127.0.0.1:8077/docs
python -m kbsvc.cli mcp         # MCP   → stdio
```

## 设计要点

**一切 id 由内容派生，不随机。** `document_id = uuid5(tenant|source|external_id)`，
`chunk_id = uuid5(document|version|ordinal|chunk_hash)`。重复导入收敛到同一行、同一个
Qdrant point，不产生脏数据。文件 sha256 未变则整个跳过。

**上传只落盘 + 入队。** 解析、切分、嵌入、索引全部由 worker 按状态机执行：
`pending → parsing → chunking → embedding → indexing → completed`，失败指数退避重试，
超限进 `failed` 并保留错误。local worker 是 API 内的单线程，server worker 是独立进程；
二者都使用租约抢占任务，崩溃后任务可被重新接管。

**每个 chunk 都能追回原文。** 携带 `char_start/char_end`（对齐解析后全文）、
`heading_path`、`page_from/page_to`、`bbox`、`source_uri`、`parser_version`。
切分不跨标题边界，表格整块保留。

**混合检索在应用层融合。** dense 与 sparse 各自查询后用 RRF 合并——两个索引本就不在一处，
local 与 server 行为因此完全一致，并且每一路的原始命中都能在 `debug` 里看到。

**词法检索为文言文而写。** 分词是自己的（字符 1-gram + 2-gram，拉丁文整词小写），打分和
倒排交给 Tantivy。不依赖分词器（现代分词器切文言文很不准），不下载模型，离线可用。

**繁简与旧字形折叠，但不改写原文。** 语料是混排的，不折叠时查 `阴阳` 与查 `陰陽` 结果完全
不相交（实测重合 0/10，折叠后 10/10）。折叠只作用于送进索引和送去嵌入的文本；payload 里的
原文一个字不动，snippet 与引文保留古籍本来的字形——用简体查询能召回《天無陰陽篇》，而返回
的正文仍是繁体。映射表是严格 1:1 的，因为高亮偏移依赖长度守恒。`乾` 在保护名单里——本语料
中它是卦名（乾坤 2,749 次），折叠成 `干` 会把它和干支合并。

> 早期版本连 BM25 也是自己实现的，编码成 Qdrant 稀疏向量。但嵌入式 Qdrant 客户端没有稀疏
> 索引，检索时用纯 Python 逐段算分——22,659 段的语料上单次查询 5.6 秒。换成 Tantivy 后是
> 2.5 毫秒，同时删掉了自研 BM25、两张语料统计表和一处有碰撞风险的词项哈希。分词器留下了：
> 那是领域知识，Tantivy 自带的 ngram 分词器会把 `hybrid` 也切成字符。

**MCP 只读且最小权限。** 只有 `search_knowledge` / `fetch_document_chunks` /
`list_sources`。租户与 ACL 由服务端凭据推导，客户端参数无法覆盖——被提示注入的 agent
也无法扩大自己的可见范围。

## 可插拔点

| 维度 | 默认 | 可选 |
|---|---|---|
| 解析 | `text`（txt/md 内置） | `docling`（PDF/Office）→ `unstructured` → `marker` 自动降级，三者默认安装 |
| 稠密嵌入 | `hash`（离线确定性） | **`fastembed`（本地 ONNX，本仓库已切到 bge-small-zh-v1.5 / 512 维）**、`openai`（任意兼容端点） |
| 重排 | `lexical`（离线） | `none`、`cross-encoder` |
| 存储 | 本地 FS | S3 / MinIO |
| 元数据 | SQLite | PostgreSQL |

Docling、Unstructured 与 Marker 随后端默认安装；它们在当前本地解析链中不需要第三方 API。
可选基础设施依赖：`pip install '.[fastembed]' '.[postgres,s3]'`。
`pgvector` / `pg-search` 复用 `.[postgres]` 里的 `psycopg`。
部分 PDF/OCR 首次解析会下载本地模型权重，因此生产环境应允许首次下载或预热模型缓存。

> `hash` 稠密嵌入不是语义模型，是字符 n-gram 的确定性随机投影。它保证零配置可跑通、
> 可测试；语义召回由 `fastembed`/`openai` 提供。词法侧无论如何都是真实 BM25。

切换模型后用 `kbsvc reembed` 重建向量——chunk_id、字符偏移、heading_path 都与模型无关，
所以**不重新解析原文件**。词法索引与嵌入模型无关，若只需重建它（例如从旧版本升级，或
`/v1/stats` 显示 `lexical_docs` 与 `chunks` 不一致），用 `kbsvc rebuild-lexical`，几十秒
而不是几分钟。详见 [runbook 第 8 节](docs/04-runbook.md)。

## 接口

| | |
|---|---|
| `POST /v1/search` | 混合检索，返回 chunk_id / score / rerank_score / page / source_uri / snippet / highlights |
| `GET /v1/documents/{id}/chunks` | 按序读连续 chunk（取上下文） |
| `POST /v1/ingest/upload` · `/path` | 登记 + 入队，202 返回 |
| `GET /v1/jobs` · `POST /v1/jobs/{id}/retry` | 任务观测与重试 |
| `GET /v1/stats` · `/healthz` · `/readyz` | 运维 |

`"debug": true` 会返回改写后的查询、每一路检索的原始命中与排名、融合前后次序、
rerank 分数、下发的 filter、各阶段耗时。调不动的检索系统等于没有。

`dense_search` 与 `sparse_search` 两个耗时字段只在对应路真正跑过时出现，所以它也是
`mode=hybrid|dense|sparse` 是否生效的最直接证据。

## 文档

- [docs/01-architecture.md](docs/01-architecture.md) — 分层、profile、状态机、检索链路
- [docs/02-data-model.md](docs/02-data-model.md) — 全部表结构、Qdrant payload 与 Tantivy 文档
- [docs/03-api.md](docs/03-api.md) — REST 与 MCP 契约
- [docs/04-runbook.md](docs/04-runbook.md) — 部署、排障、备份、MCP 客户端接入
- [docs/05-performance.md](docs/05-performance.md) — 实测吞吐、瓶颈定位、扩容时机
- [docs/06-code-architecture.md](docs/06-code-architecture.md) — 代码架构分析（依赖 DAG、复杂度热点、调用链）
  · [HTML 版](docs/06-code-architecture.html)

## 开发

```bash
python -m pytest -q --cov       # 107 tests, 85% 覆盖
python -m ruff check .
```

## 前端

[../knowledge-web/](../knowledge-web/) 是配套的 Flask 前端：检索、阅读器、书库、导入、
任务监控，以及把 `debug` 负载可视化的检索轨迹抽屉。它通过 HTTP 调用本服务，不直连数据库
或 Qdrant。

# kbsvc

自托管知识检索系统 —— 多格式文档批量导入、可追溯切分、混合检索，通过 **REST** 与 **MCP** 暴露统一查询能力。

```
文件 ──▶ 导入控制面 ──▶ 解析(Docling/可插拔) ──▶ 结构化切分 ──▶ 稠密+稀疏向量 ──▶ Qdrant
                │                                                                    │
                └── source/document/version/job 状态机、重试、索引事件                 │
                                                                                     ▼
                                          REST /v1/search  ◀── 检索管线 ──▶  MCP search_knowledge
                                                                (rewrite→fuse→rerank→citation)
```

## 为什么能直接跑

默认 `local` profile **不需要 Docker、不需要外部服务、不下载任何模型**：

| 组件 | local（默认） | server |
|---|---|---|
| 元数据 | SQLite | PostgreSQL |
| 对象存储 | 本地文件系统 | S3 / MinIO |
| 向量库 | Qdrant 嵌入式 | Qdrant 服务 |
| 队列 | 元数据库 + 租约 | 同左（可水平扩 worker） |

切换只改 `.env`。`deploy/docker-compose.yml` 已备好 server profile。

## 60 秒上手

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"

export KB_DATA_DIR="$PWD/.kbdata"
python -m kbsvc.cli init
python -m kbsvc.cli ingest ../book --source guji --patterns "*.txt,*.md"
python -m kbsvc.cli search "贼克如何取用神" --top-k 5
```

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
超限进 `failed` 并保留错误。worker 用租约抢占任务，崩溃后任务自动被接管。

**每个 chunk 都能追回原文。** 携带 `char_start/char_end`（对齐解析后全文）、
`heading_path`、`page_from/page_to`、`bbox`、`source_uri`、`parser_version`。
切分不跨标题边界，表格整块保留。

**混合检索在应用层融合。** dense 与 sparse 各自查询后用 RRF 合并，而非交给 Qdrant——
这样 local 与 server 行为完全一致，并且每一路的原始命中都能在 `debug` 里看到。

**稀疏检索为文言文而写。** 字符 1-gram + 2-gram + BM25，IDF 存在元数据库里、索引侧与
查询侧共用。不依赖分词器（现代分词器切文言文很不准），不下载模型，离线可用。

**MCP 只读且最小权限。** 只有 `search_knowledge` / `fetch_document_chunks` /
`list_sources`。租户与 ACL 由服务端凭据推导，客户端参数无法覆盖——被提示注入的 agent
也无法扩大自己的可见范围。

## 可插拔点

| 维度 | 默认 | 可选 |
|---|---|---|
| 解析 | `text`（txt/md 内置） | `docling`(默认 PDF/Office) → `unstructured` → `marker` 自动降级 |
| 稠密嵌入 | `hash`（离线确定性） | **`fastembed`（本地 ONNX，本仓库已切到 bge-small-zh-v1.5 / 512 维）**、`openai`（任意兼容端点） |
| 重排 | `lexical`（离线） | `none`、`cross-encoder` |
| 存储 | 本地 FS | S3 / MinIO |
| 元数据 | SQLite | PostgreSQL |

装可选依赖：`pip install '.[docling]' '.[fastembed]' '.[postgres,s3]'`

> `hash` 稠密嵌入不是语义模型，是字符 n-gram 的确定性随机投影。它保证零配置可跑通、
> 可测试；语义召回由 `fastembed`/`openai` 提供。稀疏侧无论如何都是真实 BM25。

切换模型后用 `kbsvc reembed` 重建向量——chunk_id、字符偏移、heading_path 都与模型无关，
所以**不重新解析原文件**。详见 [runbook 第 8 节](docs/04-runbook.md)。

## 接口

| | |
|---|---|
| `POST /v1/search` | 混合检索，返回 chunk_id / score / rerank_score / page / source_uri / snippet / highlights |
| `GET /v1/documents/{id}/chunks` | 按序读连续 chunk（取上下文） |
| `POST /v1/ingest/upload` · `/path` | 登记 + 入队，202 返回 |
| `GET /v1/jobs` · `POST /v1/jobs/{id}/retry` | 任务观测与重试 |
| `GET /v1/stats` · `/healthz` · `/readyz` | 运维 |

`"debug": true` 会返回改写后的查询、每一路检索的原始命中与排名、融合前后次序、
rerank 分数、下发给 Qdrant 的 filter、各阶段耗时。调不动的检索系统等于没有。

## 文档

- [docs/01-architecture.md](docs/01-architecture.md) — 分层、profile、状态机、检索链路
- [docs/02-data-model.md](docs/02-data-model.md) — 全部表结构与 Qdrant payload
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

# kbsvc 架构方案

自托管知识检索系统 + MCP 服务。目标：多格式文档批量导入 → 可追溯切分 → 稠密/稀疏/混合检索 → REST 与 MCP 统一查询。

## 1. 分层

```
                       ┌──────────────────────────────┐
   Claude / Codex /    │  MCP (FastMCP)               │
   IDE / Agent  ──────▶│  search_knowledge            │
                       │  fetch_document_chunks       │
                       │  list_sources                │
                       └───────────┬──────────────────┘
                                   │ 复用同一 RetrievalService
   HTTP 客户端 ────────▶┌──────────▼──────────────────┐
                       │  REST API (FastAPI)          │
                       │  /v1/search /v1/documents    │
                       │  /v1/ingest  /v1/jobs        │
                       └───────────┬──────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
┌───────────────┐        ┌──────────────────┐       ┌──────────────────┐
│ 导入控制面     │        │ 检索管线          │       │ 索引事件          │
│ source/doc/   │        │ rewrite→embed→   │       │ upsert/delete/   │
│ version/job   │        │ search→fuse→     │       │ superseded       │
│ 状态机+重试    │        │ rerank→citation  │       │                  │
└───────┬───────┘        └────────┬─────────┘       └──────────────────┘
        │                         │
   ┌────▼─────┬──────────────┐    │
   ▼          ▼              ▼    ▼
┌────────┐ ┌──────────┐ ┌─────────────┐
│ 元数据  │ │ 对象存储  │ │ 向量库       │
│ PG/SQLite│ │ S3/MinIO │ │ Qdrant      │
│         │ │ /LocalFS │ │ (server/嵌入)│
└────────┘ └──────────┘ └─────────────┘
```

## 2. 两套 Profile（同一份代码）

| 组件 | `local`（默认，零外部依赖） | `server`（生产） |
|---|---|---|
| 元数据库 | SQLite | PostgreSQL 16 |
| 对象存储 | 本地文件系统 | S3 / MinIO |
| 向量库 | Qdrant 嵌入式（`path=`） | Qdrant 服务（gRPC/HTTP） |
| 队列 | 元数据库中的 `ingest_job` 表 + 租约（lease） | 同左（同一实现，可水平扩 worker） |

切换只改 `.env`，不改代码。`server` 的 `deploy/docker-compose.yml` 已就绪。
选择"数据库即队列 + 租约"而不是 Redis/Celery：worker 崩溃后租约到期任务自动回收，无额外中间件，且 SQLite/PG 语义一致。

## 3. 核心不变量

1. **document_id 稳定**：`uuid5(NS_DOC, tenant_id|source_id|external_id)`。同一路径重复导入永远得到同一个 id。
2. **content_hash 决定是否新版本**：文件 sha256 未变 → 跳过（幂等导入）；变了 → 新建 `document_version`，旧版本置 `superseded` 并产生删除事件。
3. **chunk_id 幂等**：`uuid5(NS_CHUNK, document_id|version|ordinal|chunk_content_hash)`。Qdrant 以此为点 id upsert，重复执行不产生脏数据。
4. **可追溯**：每个 chunk 必须携带 `page_from/page_to`、`heading_path`、`section_id`、`char_start/char_end`、`bbox`（若解析器提供）、`source_uri`、`parser`/`parser_version`。
5. **租户与 ACL 前置**：所有查询强制注入 `tenant_id` 过滤；`acl` 为标签列表，查询侧传 `acl_any`，Qdrant 侧做过滤，不在应用层后过滤（避免 top_k 被吃掉）。

## 4. 导入状态机

```
                    ┌──────────────────────────────────────┐
                    ▼                                      │
 received ──▶ pending ──(lease)──▶ parsing ──▶ chunking ──▶ embedding ──▶ indexing ──▶ completed
                    ▲                 │            │            │             │
                    │                 └────────────┴────────────┴─────────────┘
                    │                              │ 异常
                    │                              ▼
                    └──────── retry_wait ◀── attempts < max ?  ──no──▶ failed
                              (指数退避)
```

- 上传接口**只做三件事**：落盘对象存储、算 content hash、写 `ingest_job(pending)`，立刻返回 202。
- worker 用 `UPDATE ... WHERE state='pending' AND (lease_expires_at IS NULL OR lease_expires_at < now)` 抢占租约，避免多 worker 重复执行。
- 每次状态跃迁写 `updated_at`，失败写 `last_error` + `attempts+1`，退避 `base * 2^attempts`（封顶）。
- 任务类型：`ingest` / `reindex` / `delete`。删除也走 worker，保证向量与元数据最终一致。

## 5. 解析：统一中间模型

`Parser` 协议 → `ParsedDocument`：

```
ParsedDocument
 ├─ meta: title, lang, page_count, parser, parser_version
 ├─ sections: [Section(id, level, heading, heading_path, page, char_span)]
 ├─ blocks:   [Block(kind=text|table|figure|code, text, section_id, page, bbox)]
 └─ tables:   [Table(id, section_id, page, bbox, rows, markdown)]
```

解析器注册表按 mime/扩展名选择，默认 **Docling**，可插拔 fallback：

| 优先级 | 解析器 | 覆盖 |
|---|---|---|
| 1 | `text`（内置） | .txt/.md（纯文本，零依赖，本仓库古籍语料走这条） |
| 2 | `docling` | pdf/docx/pptx/xlsx/html/图片（默认） |
| 3 | `unstructured` | docling 失败时的兜底 |
| 4 | `marker` | 学术 PDF 高保真兜底 |

fallback 链可配置 `KB_PARSER_CHAIN=docling,unstructured,marker`，逐个尝试，记录最终 `parser`/`parser_version` 到 version 表与 chunk payload。Docling/Unstructured/Marker 是**可选 extras**，不装也能跑（纯文本链路完整可用）。

## 6. 切分：结构感知 + 可追溯

`StructuralChunker`：
1. 以 Section 为一级边界，绝不跨 heading 合并。
2. 段内按 token 目标（默认 512，overlap 64）滑窗；中文按字符估 token（`chars/1.6`）。
3. 表格整体成块（不切碎），`kind='table'`，携带 markdown。
4. 太短的块（< min_chars）向后合并，避免碎片。
5. 每块回填 `char_start/char_end`（对齐原文）、`page_from/page_to`、`heading_path`。

## 7. 向量与检索

**Qdrant collection**（命名向量）：
- `dense`: 可配维度，Cosine
- `sparse`: 稀疏向量（BM25 权重）
- payload 索引：`tenant_id` / `document_id` / `version` / `source_id` / `acl` / `is_current`

**稀疏编码器**（自研，专为中文古籍）：字符 1-gram + 2-gram，IDF 从语料统计持久化到元数据库，查询侧复用同一词表 → 对文言文召回显著优于分词器方案，且**完全离线、无模型下载**。

**稠密编码器**（可插拔 `KB_DENSE_PROVIDER`）：
- `hash`：确定性哈希嵌入，零依赖，默认值，保证开箱即跑与可测试
- `fastembed`：ONNX 本地模型（如 `BAAI/bge-small-zh-v1.5`）
- `openai`：任意 OpenAI 兼容 `/v1/embeddings` 端点

**融合**在应用层做（不依赖 Qdrant 版本特性，local/server 行为一致）：dense 与 sparse 各取 `top_k * overfetch`，RRF（默认 k=60，可加权）合并。

**重排**：`KB_RERANKER` = `none` | `lexical`（默认，字符 n-gram 覆盖率 + 位置加权，离线）| `cross-encoder`（可选 extra）。

**引用拼装**：输出 `snippet`（命中片段窗口）+ `highlights`（命中 span），`source_uri` 带 `#page=` 锚点。

## 8. 检索调试信息

`debug=true` 时返回：`rewritten_queries`、每路检索的原始 hits（id+score+rank）、融合前后的排名变化、rerank 前后分数、各阶段耗时 ms、实际下发给 Qdrant 的 filter。这是这套系统能被调优的前提，属于一等公民而非日志。

## 9. MCP 暴露面（首版最小权限）

| 工具 | 权限 | 说明 |
|---|---|---|
| `search_knowledge` | 只读 | 混合检索，返回带引用的 chunk |
| `fetch_document_chunks` | 只读 | 按 document_id 取连续 chunk（读上下文用） |
| `list_sources` | 只读 | 列出可见 source |

**不暴露**任何写入/删除工具。传输：本地 `stdio`，远程 `streamable-http`（带 API Key）。租户与 ACL 由服务端从凭据推导，客户端**不可**自行指定 tenant_id。

## 10. 安全

- API Key → tenant + acl 标签集合（`api_key` 表，存 sha256）。
- 所有检索强制 tenant filter，ACL 在向量库侧过滤。
- 上传限制 mime 白名单 + 大小上限；对象 key 用 hash 派生，杜绝路径穿越。
- 错误响应统一信封，不回显内部路径/堆栈。

## 11. 目录

```
src/kbsvc/
  config.py  ids.py  errors.py  logging.py
  models/     ir.py  events.py
  db/         models.py  session.py  repo.py
  storage/    base.py  local.py  s3.py
  parsing/    base.py  registry.py  text_parser.py  docling_parser.py
              unstructured_parser.py  marker_parser.py
  chunking/   base.py  tokenizer.py  structural.py
  embedding/  base.py  dense_hash.py  dense_fastembed.py  dense_openai.py  sparse_bm25.py
  vector/     base.py  qdrant_store.py
  retrieval/  rewrite.py  fusion.py  rerank.py  citation.py  pipeline.py
  ingest/     states.py  uploader.py  worker.py
  api/        app.py  deps.py  auth.py  schemas.py  routers/*.py
  mcp/        server.py
  cli.py
```

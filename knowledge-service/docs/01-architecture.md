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
   ┌────▼─────┬──────────────┬──────────────┐
   ▼          ▼              ▼              ▼
┌────────┐ ┌──────────┐ ┌─────────────┐ ┌─────────────┐
│ 元数据  │ │ 对象存储  │ │ 向量库       │ │ 词法索引     │
│PG/SQLite│ │ S3/MinIO │ │ Qdrant      │ │ Tantivy     │
│         │ │ /LocalFS │ │ (dense)     │ │ (BM25)      │
└────────┘ └──────────┘ └─────────────┘ └─────────────┘
```

检索的两路各有其存储：稠密向量在 Qdrant，词法倒排在 Tantivy。两者都是嵌入式的，
`local` profile 因此仍然不需要 Docker。

## 2. 两套 Profile（同一份代码）

| 组件 | `local`（默认） | `server`（生产） |
|---|---|---|
| 元数据库 | SQLite | PostgreSQL 16 |
| 对象存储 | 本地文件系统 | S3 / MinIO |
| 向量库 | API 进程内嵌 Qdrant（本地目录） | Qdrant Server（HTTP） |
| 词法索引 | 嵌入式 Tantivy（本地目录） | 嵌入式 Tantivy（本地目录） |
| 队列 | SQLite `ingest_job` + API 内置 worker 线程 | PostgreSQL `ingest_job` + 可水平扩 worker |

词法索引两种 profile 相同——Tantivy 是进程内的 Rust 库，没有服务端形态。代价是
`server` profile 下多个 worker 进程不能共写同一个索引目录（Tantivy 持有目录锁）。

`local` 不依赖 Docker。SQLite、原始文件、Qdrant 与 Tantivy 数据均在 `KB_DATA_DIR`；
根目录 `run.bat` 只需编排 API 与 Web：

```text
Web ──HTTP──▶ API 进程
              ├── SQLite / LocalFS
              ├── embedded Qdrant（进程级单例）
              ├── Tantivy 索引（进程级单例，持目录锁）
              └── 常驻 worker 线程 ◀── ingest_job
```

API 请求线程与 worker 线程共享进程级 Qdrant 单例；适配器允许跨线程访问并用可重入锁
串行化嵌入式读写，因此只有一个进程持有目录锁，Web 上传后仍可自动处理。该模式只允许
一个 API 进程，不支持本地多 worker；需要多进程或扩容时切换完整 `server` profile 的
PostgreSQL、S3/MinIO、Qdrant Server 与独立 worker。

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

fallback 链可配置 `KB_PARSER_CHAIN=docling,unstructured,marker`，逐个尝试，记录最终 `parser`/`parser_version` 到 version 表与 chunk payload。Docling、Unstructured 与 Marker 是后端核心依赖，标准安装和 Docker 镜像都会安装；三者均在本地进程中解析，不要求第三方解析 API。

## 6. 切分：结构感知 + 可追溯

`StructuralChunker`：
1. 以 Section 为一级边界，绝不跨 heading 合并。
2. 段内按 token 目标（默认 512，overlap 64）滑窗；中文按字符估 token（`chars/1.6`）。
3. 表格整体成块（不切碎），`kind='table'`，携带 markdown。
4. 太短的块（< min_chars）向后合并，避免碎片。
5. 每块回填 `char_start/char_end`（对齐原文）、`page_from/page_to`、`heading_path`。

## 7. 向量与检索

检索是两个独立的索引，各自回答同一个问题，再在应用层融合。

**Qdrant collection**（稠密）：
- `dense`: 可配维度，Cosine
- payload 索引：`tenant_id` / `document_id` / `version` / `source_id` / `acl` / `is_current`

**Tantivy 索引**（词法）：Rust 倒排索引，BM25 打分。字段：`chunk_id`（raw，删除按 term）、
`body`（打分字段，`freq` 而非 `position`——不做短语查询，省索引体积）、`payload`（stored，
命中直接带回渲染所需数据，稀疏单路检索无需二次查询）、以及 `tenant_id` / `document_id` /
`version_id` / `source_id` / `kind` / `acl` / `is_current` 这些过滤字段。

**分词器**（自研，专为中文古籍，`lexical/tokenizer.py`）：字符 1-gram + 2-gram，拉丁文整词
小写。对文言文召回显著优于分词器方案，且完全离线、无模型下载。语料在进入 Tantivy 前先经
这个函数切好并以空格连接，Tantivy 自身的分析器只需按空格切分——**语料学交给我们，索引与
打分交给引擎**。

**字形归一化**（`normalize.py`，`KB_NORMALIZE_CJK`，默认开）：语料是混排的——14.3M 字里
简体为主，但繁体与旧字形贯穿全篇（陰 524 次、發 512 次、陽 402 次）。

实测（同一语料、同一批查询，top-10 重合度）：

| 查询对 | 词法 · 归一化前 | 词法 · 后 | 稠密 · 前 | 稠密 · 后 |
|---|---|---|---|---|
| 阴阳 / 陰陽 | **0/10** | 10/10 | 4/10 | 10/10 |
| 遥克 / 遙克 | **0/10** | 10/10 | 4/10 | 10/10 |
| 万 / 萬 | **0/10** | 10/10 | 2/10 | 10/10 |

不归一化时两种字形的检索结果**完全不相交**。

三个设计约束：

1. **只作用于检索表示，绝不改写存储文本。** payload 里的 `text` 是渲染 snippet 与引文
   的依据，改写古籍原文等于篡改。
2. **必须长度守恒。** `citation.build_snippet` 用 `str.find` 定位高亮并返回偏移量；若归一化
   会改变长度，偏移会静默漂移。因此表是自建的**严格 1:1 字符映射**（4,300 条，从 zhconv 的
   `zh-hans` 逐字提取并校验），而不是整串调用 `zhconv.convert`。用 `zh-hans` 而非 `zh-cn`，
   因为后者会做大陆词汇替换（軟體 → 软件）——改写古籍的用词不是我们该做的事。
3. **有领域保护名单。** zhconv 把 `乾` 折叠成 `干`（取「干燥」义），但本语料里 `乾` 全是卦名
   （乾坤 2,749 次、乾卦、乾元、乾道，与巽/艮/坤同现），折叠会把**乾坤与干支合并**——两个
   核心且无关的概念。`乾` 因此进入 `_PROTECTED`。这是查过语料才定的，不是想当然。

两路都做：词法侧在 `tokenize()` 内部折叠（索引与查询共用同一入口，天然一致）；稠密侧用
`FoldingDenseEmbedder` 包装嵌入器，`embed_documents` 与 `embed_query` 一起折叠——那里有
三个调用点（worker / reembed / pipeline），包装比在每处写一遍更难出错。

> 早期版本把 BM25 自己实现了：文档端存长度归一化 TF、查询端存 IDF，编码成 Qdrant 稀疏
> 向量，语料统计维护在 `term_stat` / `corpus_stat` 两张表里。它是对的，但嵌入式 Qdrant
> 客户端没有稀疏索引，检索时用纯 Python 逐个 chunk 算分——22,659 段的语料上单次查询
> 5.6 秒。改用 Tantivy 后是 2–4 毫秒，同时删掉了自研 BM25、两张统计表，以及把词项哈希
> 到 31 位空间的 `crc32`（那里有静默的碰撞风险）。

**稠密编码器**（可插拔 `KB_DENSE_PROVIDER`）：
- `hash`：确定性哈希嵌入，零依赖，默认值，保证开箱即跑与可测试
- `fastembed`：ONNX 本地模型（如 `BAAI/bge-small-zh-v1.5`）
- `openai`：任意 OpenAI 兼容 `/v1/embeddings` 端点

**融合**在应用层做（两个索引本就不在一处，local/server 行为一致）：dense 与 sparse 各取
`top_k * overfetch`，RRF（默认 k=60，可加权）合并。

**查询改写与两路的关系**：`rewrite` 只做减法（去句尾标点、剥开头框架词、按标点切分），
所以每个变体都是原查询的连续子串，字符 n-gram 必是原查询的子集。词法一路因此把所有变体
合并成一次查询即可，不损失任何召回；稠密一路保留逐变体检索，因为那里变体嵌入到不同的点，
改写真正有价值。

**重排**：`KB_RERANKER` = `none` | `lexical`（默认，字符 n-gram 覆盖率 + 位置加权，离线）| `cross-encoder`（可选 extra）。

**引用拼装**：输出 `snippet`（命中片段窗口）+ `highlights`（命中 span），`source_uri` 带 `#page=` 锚点。

## 8. 检索调试信息

`debug=true` 时返回：`rewritten_queries`、每路检索的原始 hits（id+score+rank）、融合前后的排名变化、rerank 前后分数、各阶段耗时 ms、下发的 filter。这是这套系统能被调优的前提，属于一等公民而非日志。

注意 `dense_search` 与 `sparse_search` 两个耗时字段只在对应路真正跑过时出现——这也是
`mode` 是否生效的最直接证据。

## 9. MCP 暴露面（首版最小权限）

| 工具 | 权限 | 说明 |
|---|---|---|
| `search_knowledge` | 只读 | 混合检索，返回带引用的 chunk |
| `fetch_document_chunks` | 只读 | 按 document_id 取连续 chunk（读上下文用） |
| `list_sources` | 只读 | 列出可见 source |

**不暴露**任何写入/删除工具。传输：本地 `stdio`，远程 `streamable-http`（带 API Key）。租户与 ACL 由服务端从凭据推导，客户端**不可**自行指定 tenant_id。

## 10. 安全

- API Key → tenant + acl 标签集合（`api_key` 表，存 sha256）。
- 所有检索强制 tenant filter，ACL 在两个索引侧各自过滤（Qdrant payload filter / Tantivy term query），不在应用层后过滤。
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
  normalize.py                       # 繁简/旧字形折叠，两路共用
  chunking/   base.py  tokenizer.py  structural.py
  embedding/  base.py  dense_hash.py  dense_fastembed.py  dense_openai.py
  vector/     base.py  qdrant_store.py          # 稠密
  lexical/    base.py  tantivy_store.py  tokenizer.py   # 词法
  retrieval/  rewrite.py  fusion.py  rerank.py  citation.py  pipeline.py
  ingest/     states.py  uploader.py  worker.py
  api/        app.py  deps.py  auth.py  schemas.py  routers/*.py
  mcp/        server.py
  cli.py
```

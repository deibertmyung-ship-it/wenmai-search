# kbsvc 代码架构分析

> 本文的结论来自代码知识图谱的静态分析（879 节点 / 4208 边），不是对设计意图的复述。
> 复杂度、循环深度、依赖方向、环检测均为实测值。
> 索引名 `kbsvc`，索引时间 2026-08-02。
>
> **⚠ 图谱数据早于「自研 BM25 → Tantivy」的重构。** 规模一节的行数与文件数已按当前代码
> 重测；复杂度、社区检测、图节点/边数尚未重跑，涉及已删除代码的条目已就地标注。重跑
> 索引后请更新本文。

## 1. 规模

| 维度 | 数值 | 时点 |
|---|---|---|
| 生产代码 | 63 个 Python 文件，4625 行 | 重构后重测 |
| 测试代码 | 119 个用例 | 重构后重测 |
| 图节点 | 879（Function 253 / Method 148 / Class 95 / Route 25） | 重构前 |
| 图边 | 4208（DEFINES 1552 / USAGE 1085 / CALLS 605 / IMPORTS 256） | 重构前 |
| HTTP 路由 | 25 条 | 重构前 |

重构净减约 700 行：删掉自研 BM25（`embedding/sparse_bm25.py` 117 行）、`repo.py` 的词表
统计维护（110 行）、`worker.py` 的统计增量方法，新增 `lexical/` 包 4 个文件。

最大的五个文件（重构后重测）：

| 文件 | 行数 | 职责 |
|---|---|---|
| `cli.py` | 290 | 运维入口（10 个子命令） |
| `ingest/worker.py` | 283 | 状态机执行器 |
| `lexical/tantivy_store.py` | 269 | 词法索引适配，含 Windows 句柄与批量装载的特殊处理 |
| `db/repo.py` | 264 | 全部多行读写 |
| `vector/qdrant_store.py` | 247 | 向量库适配，含嵌入式模式的特殊处理 |

没有文件超过 800 行的内部约定线。原先 `repo.py` 以 429 行居首，体量主要来自 BM25 词表
维护；那段代码已随自研 BM25 一并删除，`repo.py` 降到 264 行，**曾建议的 `db/term_stats.py`
拆分因此作废**。现在最大的是 `cli.py`，属于命令罗列型增长，不构成结构问题。

## 2. 包依赖：无环 DAG

对 `src/kbsvc/` 下所有相对导入做 AST 提取后做环检测，**结果是 NONE**——依赖图是严格的有向无环图。

```
                        ┌──────────────────────────────┐
   出度 10（最宽）        │  ingest                      │  编排层
                        │  uploader / worker / reembed │
                        │  states                      │
                        └──┬────┬────┬────┬────┬───┬───┘
                           │    │    │    │    │   │
        ┌──────────────────┘    │    │    │    │   └──────────┐
        ▼                       ▼    ▼    ▼    ▼              ▼
   ┌─────────┐          ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐
   │ parsing │          │ chunking │ │embedding│ │ vector │ │storage │  能力层
   └────┬────┘          └────┬─────┘ └───┬────┘ └───┬────┘ └───┬────┘
        │                    │           │          │          │
        └────────┬───────────┴─────┬─────┴──────────┴──────────┘
                 ▼                 ▼
           ┌──────────┐      ┌──────────┐
           │  models  │      │    db    │                              基础层
           │  ids     │      └────┬─────┘
           └──────────┘           │
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
           ┌──────────┐                      ┌──────────┐
           │  config  │  入度 11              │  errors  │  入度 6      叶子
           └──────────┘                      └──────────┘
```

实测的出入度（`out` = 依赖了几个包，`in` = 被几个包依赖）：

| 包 | out | in | 依赖 |
|---|---|---|---|
| `ingest` | **10** | 2 | chunking, config, db, embedding, errors, models, parsing, storage, vector |
| `api` | 7 | 2 | config, db, errors, ingest, retrieval, vector |
| `retrieval` | 4 | 3 | config, db, embedding, vector |
| `mcp` | 4 | 1 | **api**, config, db, retrieval |
| `parsing` | 3 | 1 | config, errors, models |
| `chunking` | 3 | 1 | config, ids, models |
| `embedding` | 3 | 2 | config, db, errors |
| `db` | 3 | 6 | config, models |
| `vector` | 2 | 4 | config, errors |
| `storage` | 2 | 1 | config, errors |
| `config` | 0 | **11** | — |
| `errors` | 0 | 6 | — |
| `models` | 0 | 4 | — |
| `ids` | 0 | 1 | — |

**三条可以从数字直接读出的事实：**

1. **`ingest` 是唯一的编排者**（out=10, in=2）。它是把解析、切分、嵌入、向量写入、元数据事务缝在一起的地方，也是唯一同时触碰全部能力层的包。这不是耦合失控——状态机本来就需要看到全景。反过来说，任何"新增一种导入方式"的改动都会落在这一个包里。

2. **`config` / `errors` / `models` / `ids` 是纯叶子**（out=0）。它们不依赖任何内部包，因此可以被任意模块安全引入而不产生环。`config` 入度 11 意味着几乎每个包都读配置——这是设计选择（配置驱动的可插拔），代价是改配置结构会波及面很广。

3. **`mcp` 依赖 `api`**——这是全图里唯一一条"暴露层依赖暴露层"的边。原因是 `mcp/server.py` 复用了 `api/auth.py:resolve_principal` 来从 API Key 推导租户与 ACL。这是刻意的：如果 MCP 自己实现一套凭据解析，两条暴露路径的权限语义就会漂移。代价是 MCP 不能脱离 API 模块单独打包。

## 3. 社区检测：真实的模块边界

Leiden 社区检测在调用图上跑出的簇，与目录结构**并不完全重合**——这才是实际的架构接缝：

| 簇 | 内聚度 | 代表成员 | 说明 |
|---|---|---|---|
| 40 | 0.53 | `_run_ingest`, `_run_delete`, ~~`_apply_stats_delta`~~, `_supersede_older`, `_build_points` | 状态机执行路径。**完全落在 worker.py 内部**，无外部成员——这个类是自洽的。（`_apply_stats_delta` 已随自研 BM25 删除） |
| 15 | 0.69 | ~~`encode_query`, `encode_document`, `term_frequencies`~~ | ~~BM25 稀疏编码~~ — **整簇已消失**：打分交给 Tantivy，只剩 `lexical/tokenizer.py` 的 `tokenize`/`analyze` 两个纯函数 |
| 28 | **1.00** | `_path`, `put`, `exists`, `get` | 对象存储。满内聚，接口极窄 |
| 7 | 0.76 | `search`, `get_settings`, `get_dense_embedder`, `search_command` | 检索入口横跨 pipeline / config / embedding / cli |
| 22 | 0.61 | `ingest_path`, `_purge_local_collection`, `exists`, `close` | 资源生命周期管理，横跨 vector 与 api |

簇 28（对象存储）内聚度 1.0，说明 `ObjectStore` 协议的抽象是干净的：4 个方法自成闭环，换 S3 实现不会牵动别处。这与 `storage` 包 out=2 / in=1 的极低耦合互相印证。

## 4. 复杂度热点

对 `src/` 下所有函数做圈复杂度、认知复杂度、循环深度与跨过程传递循环深度的度量，超阈值的如下（按认知复杂度排序）：

| 函数 | 文件 | 圈复杂度 | 认知 | 循环深度 | 传递循环深度 | 行数 |
|---|---|---|---|---|---|---|
| `_run_retrievers` | retrieval/pipeline.py | 9 | **22** | 2 | 2 | 38 |
| `decode_bytes` | parsing/text_parser.py | 9 | 18 | 1 | 1 | 22 |
| `ingest_command` | cli.py | 9 | 17 | 1 | **9** | 65 |
| `parse` | parsing/unstructured_parser.py | 10 | 15 | 1 | 1 | 45 |
| ~~`bump_term_stats`~~ | ~~db/repo.py~~ | 7 | 14 | 2 | 5 | 46 | ← **已删除** |
| `reembed_tenant` | ingest/reembed.py | 9 | 14 | 1 | 1 | 85 |
| `build_from_markdown` | parsing/base.py | 6 | 13 | 2 | 2 | 86 |
| `reciprocal_rank_fusion` | retrieval/fusion.py | 5 | 12 | 2 | 2 | 26 |
| `_attach_page_anchors` | parsing/docling_parser.py | 7 | 12 | 2 | 2 | 36 |
| `run_once` | ingest/worker.py | 6 | 11 | 0 | **7** | 21 |
| ~~`_apply_stats_delta`~~ | ~~ingest/worker.py~~ | 3 | 4 | 2 | **7** | 20 | ← **已删除** |

**值得注意的三处：**

**`_run_retrievers` 认知复杂度 22，是全项目最高。** 它要处理 3 种检索模式 × N 个改写变体 × 两路去重合并，每一路还要各自计时。这是真实的组合复杂度，不是写法问题。若要降，唯一有效的手段是把"单路检索 + 计时"抽成一个 retriever 对象，让这里只做循环——但那会引入一层间接，在只有两路的情况下未必划算。**建议：暂不重构，但增加第三路检索时必须先抽。**

> 重构后这个函数已明显变简单：词法一路不再逐变体检索、不再手工合并去重，只发一次查询。
> 复杂度未重测，但方向是下降的。

**~~`_apply_stats_delta` 自身认知复杂度只有 4，传递循环深度却是 7。~~** 已随自研 BM25 删除。
当时的结论是对的——它自己简单，但跨过程展开后（`bump_term_stats` → `_batched` → 逐批
upsert）有 7 层嵌套循环，与 [05-performance.md](05-performance.md) 实测的「词表维护是索引
阶段主要成本之一」吻合。**这条热点现在通过删除代码消除，而不是优化代码。**

**`ingest_command` 传递循环深度 9，是全项目最深。** 它是 CLI 的批量导入：遍历文件 → 每个文件走完整 ingest 链路。深度合理，但它把"文件发现 + 登记 + 排空队列"三件事写在一个 65 行函数里，与 `api/routers/ingest.py:ingest_path` 有明显重复。**建议：把文件发现逻辑提到 `ingest/discovery.py`，两个调用方共用。**

**`citation.py` 的两个函数带 `linear_scan_in_loop`**（`_find_spans`=2, `_best_window_start`=1），即循环里套了 find/contains 式扫描——隐藏的 O(n²)。但它们只作用于单个 snippet（默认 320 字符），实际耗时在实测中不到 0.5ms，**不构成问题，无需处理**。

## 5. 调用链：导入路径

`_run_ingest` 的出向调用图（3 跳，已剔除测试）：

```
_run_ingest
├─ _advance ─────────────────────► 状态跃迁 + 续租
├─ ObjectStore.get ──────────────► 取原始字节
├─ Parser.parse ─────────────────► 解析（经 registry 选链）
├─ StructuralChunker.chunk
│  ├─ _chunk_block ─► _pack ─────► 句子打包 + 偏移追踪
│  ├─ _table_chunk
│  ├─ _absorb_runts
│  └─ ParsedDocument.section_by_id
├─ _persist_chunks ──────────────► ids.chunk_id ─► ids._join
│                                  repo.replace_chunks
├─ _build_points ────────────────► DenseEmbedder.embed_documents
├─ VectorStore.ensure_collection
├─ VectorStore.upsert
├─ LexicalStore.delete_by_versions ──► 与 replace_chunks 同语义：整版本替换
├─ LexicalStore.upsert ──────────► tokenizer.analyze ─► tantivy add_document
├─ _supersede_older ─────────────► repo.superseded_version_ids
│                                  repo.delete_chunks_for_versions
│                                  VectorStore.delete_by_versions
│                                  repo.mark_versions_superseded
└─ repo.record_event
```

这条链印证了一个设计约束：**`_run_ingest` 只与协议交互，不与实现交互**——它调用的是 `ObjectStore.get`、`Parser.parse`、`VectorStore.upsert`、`DenseEmbedder.embed_documents`，全部是 Protocol 上的方法，没有一处指向具体的 `LocalObjectStore` / `TextParser` / `QdrantVectorStore`。可插拔性在调用图上是可验证的，不只是文档承诺。

## 6. 高扇入符号

| 符号 | 扇入 | 说明 |
|---|---|---|
| `ObjectStore.get` | 33 | 协议方法，被大量测试与生产路径调用 |
| `IngestWorker.drain` | 17 | 测试排空队列的标准手段 |
| `VectorStore.count` | 12 | 统计与断言 |
| `db.session.session_scope` | 10 | 事务边界，唯一的会话获取方式 |
| `db.session.init_db` | 9 | 每个入口都要先建表 |
| `ingest.reembed.reembed_tenant` | 7 | 重建入口 |
| `chunking.structural.StructuralChunker.chunk` | 7 | 切分入口 |

`session_scope` 扇入 10 且没有旁路——全项目没有直接 `sessionmaker()` 的调用点。事务边界是单一的，这是数据一致性的前提。

## 7. 分层判定

图分析给出的层次判定：

| 包 | 判定层 | 依据 |
|---|---|---|
| `api` | api | 含 HTTP 路由定义 |
| `kbsvc`(整体) | internal | fan-in=82, fan-out=124 |
| `config` / `errors` / `models` / `ids` | core | 高扇入零扇出 |

25 条路由分布在 5 个 router 模块，`app.py` 只做装配（依赖 5 个 router，被 0 个依赖）——是干净的组合根（composition root）。

## 8. 结论与建议

**结构上没有需要立即处理的问题。** 依赖无环、协议边界在调用图上可验证、事务边界单一、组合根干净。

按优先级排列的改进项：

| 优先级 | 事项 | 依据 |
|---|---|---|
| 高 | 重跑代码图谱索引 | 本文的复杂度与社区检测数据早于 Tantivy 重构 |
| 中 | 抽出 `ingest/discovery.py`，消除 CLI 与 API 的文件发现重复 | `ingest_command` 传递循环深度 9，与 `ingest_path` 逻辑重复 |
| 低 | 增加第三路检索前，先把"单路检索+计时"抽成 retriever 对象 | `_run_retrievers` 认知复杂度 22，已是全项目最高 |
| ~~中~~ | ~~BM25 词表维护若成为瓶颈，将 `_SQL_VAR_BATCH` 从 500 提到 5000~~ | **已作废**：词表维护与 `_SQL_VAR_BATCH` 均已删除 |
| ~~低~~ | ~~`repo.py` 429 行，若继续增长可拆出 `db/term_stats.py`~~ | **已作废**：`repo.py` 已降到 264 行 |

**不建议动的地方：** `citation.py` 的 O(n²) 扫描（作用域是单个 320 字符 snippet，实测 <0.5ms）；`mcp` → `api` 的依赖（刻意为之，避免权限语义漂移）；`config` 的高扇入（配置驱动架构的必然代价）。

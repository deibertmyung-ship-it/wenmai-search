# ADR-0008：检索存储收敛进各 profile 的主数据库

- 状态：Accepted
- 日期：2026-08-14
- 决策者：项目维护者
- 影响范围：`vector/`、`lexical/`、`config.py`、`ingest/worker.py`、`ingest/reembed.py`、`db/session.py`、`docker-compose.yml`
- 触发事件：Tantivy 目录锁把 server profile 永久限制在一个写 worker
- 实现提交：PR #7（双实现与生产切换）；ticket 13 删除 Qdrant/Tantivy（2026-09-19，人决定放弃剩余观察期与回滚）

## 背景

检索目前跨三套存储：元数据在 SQLite/PostgreSQL，稠密向量在 Qdrant，词法倒排在 Tantivy。两个 profile 各自组合：

```
local    SQLite + 嵌入式 Qdrant + Tantivy + LocalFS
server   PostgreSQL + Qdrant Server + Tantivy（共享目录）+ MinIO
```

### 单写者上限只由 Tantivy 造成

`lexical/tantivy_store.py:21` 记录了这条天花板：

> That directory lock also caps the deployment at exactly one writing worker process.

这是 `docs/05-performance.md:197` 扩容信号表里「单 worker 追不上导入速度」的真实阻塞点。**Qdrant 不背这个锅**——Qdrant Server 本来就处理并发写，嵌入式模式的目录锁只影响 local profile，而那里本就只有一个进程。这个分解决定了词法层的替换承载全部扩容价值。

### 发布步骤没有事务

`ingest/worker.py:138-145`：

```
138:  store.upsert(points)                              # 向量落库
144:  lexical.delete_by_versions(tenant_id, [ver.id])   # 进程死在这里
145:  lexical.upsert(...)                               # 词法落库
```

进程在两步之间崩溃，chunk 只存在于 Qdrant 而不在 Tantivy 里，静默退化成 dense-only 可召回。`reembed.py:114-115` 有同样的形状。`docs/05-performance.md:202` 用「`lexical_docs` ≠ `chunks` → 跑 `rebuild-lexical`」把它当运维现象处理，而非结构问题。

### 前提修正：ParadeDB 的 pg_search 就是 Tantivy

`pg_search` 把 Tantivy 作为 Postgres 的 index access method 嵌入。本项目现在就直接依赖 `tantivy>=0.24`。因此本决策**不改变 BM25 引擎**，只改变索引所在的进程与事务域。任何以「换到真 BM25」为由的论证都不成立。

### 规模前提

语料按 10 万段封顶（当前 22,345 段，`ADR-0002`）。本 ADR 的全部结论以此为前提；超出后需另建 ADR 重估。

### 实测数据

以下数字**测于合成夹具**（10 万段 × 512 维，202 个聚类中心与书目对齐，簇内余弦相似度 0.7），**不是真实语料**。脚本与夹具见 `.scratch/storage-consolidation/`——该目录不进版本库，故数字抄录于此。

| 项 | 值 | 口径 |
|---|---|---|
| pgvector HNSW 建索引 | 4 分 34 秒、260 MB | 实测，单线程 |
| pg_search BM25 建索引 | 3 秒 | 实测 |
| pgvector Recall@10（以精确暴力为真值） | 0.963 无过滤 / 0.963 宽过滤(90%) / 1.000 窄过滤(0.5%) | 实测，30 次查询 |
| pgvector 查询延迟 | 3.9 ms 无过滤 / 7.8 ms 窄过滤 | 实测 |
| sqlite-vec 灌数据 | 9.1 秒 | 实测 |
| sqlite-vec KNN | 102 ms 无过滤 / 46 ms 窄过滤 | 实测 |
| sqlite-vec 单点成本 | 1.02 µs | 实测，严格线性（N 增 20× 延迟增 17.3×） |
| 嵌入式 Qdrant 单点成本 | 3.5 µs | 实测，`docs/05-performance.md:131` |

**sqlite-vec 相对嵌入式 Qdrant 是 3.4×**（同口径微基准）。`docs/05-performance.md:136` 的 385 ms 是整条流水线实测、含 payload 等开销，与净向量基准不可直接比；端到端增益未测，估计落在 3.4× 到 13× 之间。

两条与预期相反的实测结果，各自导出一条必须编码的边界，见「决策」：

- **pgvector HNSW 在窄过滤下返回 0 行**（不是少返回，是空集），`hnsw.iterative_scan` 救不了（召回 8%，且比精确扫描慢 3–4 倍）。
- **SQLite WAL 下跨 ATTACH 的多库事务会撕裂**：随机硬杀 24 次撕裂 15 次；`journal_mode=DELETE` 对照组 25 次全一致。

## 决策

**把检索的两个索引收进各 profile 的主数据库，三套存储变一套；分词保持不变。**

```
local    单个 kbsvc.db：元数据 + vec0(sqlite-vec) + fts5
server   ParadeDB：元数据 + pgvector + pg_search  ／ MinIO 保留
```

`VectorStore`（`vector/base.py:45`）与 `LexicalStore`（`lexical/base.py:33`）两个 Protocol 不变，新增四个实现。全仓没有任何模块直接 import `tantivy` 或 `qdrant_client`，替换止于两个工厂函数。

### 分词保持 `analyze()` 不变

两侧共用现有的字 n-gram + 字形折叠（`lexical/tokenizer.py`）。FTS5 用 `unicode61`，pg_search 用 `whitespace`，三者对预分析文本的 term 切分逐条相等（实测 `五行` 在三个引擎中均保持整词）。

这不只是省事：**它使迁移可以用「新旧栈返回同一个排序」做自动化验收**，见「验证要求」。

### 边界一：`SearchFilter` 的每个字段都必须有 btree 索引

`tenant_id`、`document_id`、`source_id`、`acl`、`kind`、`is_current` 缺任何一个，planner 就只能走 HNSW，对应的过滤查询**静默返回空集**。ADR-0005 的「限定书名」正是 0.5% 选择率的窄过滤，是最坏情形。

补上 btree 后 planner 改走「索引取候选 → 精确排序」，召回率 1.000、7.8 ms。**这是正确性前提，不是性能优化。**

### 边界二：词法过滤走 pg_search 查询 DSL，不写 SQL `WHERE`

实测 top-10 耗时：无过滤 7.2 ms、查询 DSL 111.9 ms、SQL `WHERE` 429.3 ms。文本类过滤字段在 SQL `WHERE` 下退化成 `heap_filter`（`EXPLAIN` 中可见），布尔字段则会被下推。统一走 DSL，与 `lexical/tantivy_store.py:317-332` 现有做法同构。

### 边界三：local 必须是单文件

元数据、`vec0`、`fts5` 必须同库。SQLite 在 WAL 下不保证跨 ATTACH 的多库事务原子性（已实证），而拿到发布原子性正是本决策的收益之一。拆成两个文件会让 `worker.py:138-145` 的裂缝原样保留。

代价：批量重建与元数据写入共用一把写锁，因此 `reembed` 必须改回分批提交。`docs/05-performance.md:182` 记录的「批量路径走单次提交」是为躲 Tantivy 段合并竞争（逐批提交有 40% 概率以 `os error 5` 杀掉 writer），该理由随 Tantivy 一起消失。

### 边界四：稠密向量从 Qdrant 导出，不重嵌入

验收要求 local 稠密 top-k 集合与旧栈相等；重嵌入会引入 ONNX 非确定性与批次效应，使断言因与存储无关的原因失败。导出是逐位精确的，且是分钟级而非约 2 小时。词法不需导出，`rebuild-lexical` 从 PG 重建 21 秒（`docs/05-performance.md:190`）。

### 边界五：`docker-compose.yml` 必须设 `shm_size`

Docker 默认 `/dev/shm` 为 64 MB，pgvector 并行建 HNSW 直接失败于 `could not resize shared memory segment ... No space left on device`。

### 边界六：双实现共存，且删除条件写死

新增 `KB_VECTOR_BACKEND` 与 `KB_LEXICAL_BACKEND` 两个开关，默认旧实现。**双实现不是可选的保险，是验收的硬性前提**——A/B 断言要求新旧栈同时可加载。

删除触发条件：**A/B 全绿，且生产翻开关后运行满 14 天无回退**，即开删除 PR，同时移除 `qdrant-client` 与 `tantivy` 依赖。不写死这条，「同版删除」会静默变成永久三实现维护。

## 选择该方案的原因

- 单写者上限是当前架构唯一的结构性天花板，词法层进 Postgres 后由 MVCC 解除，`docs/05-performance.md:197` 的扩容路径才真正打开；
- 发布步骤收敛为一个事务，消除一整类静默的索引漂移；
- 组件数 server 4→2、local 3→1，local 成为真正的单文件部署，备份即拷贝一个文件；
- 在 10 万段前提下 pgvector 与 sqlite-vec 的实测指标均达标，Qdrant 的量化与 filterable HNSW 属于付了钱用不上的能力；
- local 侧顺带修掉 `docs/05-performance.md:200` 记录的「加了过滤反而更慢」——嵌入式 Qdrant 窄过滤 **+31%**，sqlite-vec 窄过滤 **−55%**，方向反转；
- Tantivy 的一整套 Windows 绕行代码（段合并抢句柄、mmap 句柄延迟释放、`_purge` 重试 5 次、写线程数被迫设为 1）随实现一起消失；
- ADR-0007 的向量召回在迁移后可写成一条 SQL：指纹 GIN 与向量同库同事务快照，而非跨存储两次往返。

## 被否决的方案

### 只替换词法层，向量保留 Qdrant

否决原因：拿到了全部扩容价值，但发布裂缝永久保留，local 也仍不是单文件。且在 10 万段前提下 Qdrant 的优势项全部用不上，为不会到来的规模长期维护一个额外服务。

### 分两步走，先词法后向量

否决原因：发布原子性要等第二步才兑现，而两步之间的中间态需要额外的验收与运维成本。风险在 spike 之后已量化到可接受，不值得为此拆分。

### server 词法用 PostgreSQL 原生 `tsvector` + GIN

`analyze()` 的输出恰好是空格分隔的 token，`to_tsvector('simple', analyzed)` 可直接索引，零额外扩展、零厂商依赖，两条驱动力同样满足。

否决原因：`ts_rank_cd` 不是 BM25，没有 tf 饱和与长度归一化。这会让「新旧栈同排序」的验收断言按定义失效，把一次结构性迁移变成没有评测集支撑的质量赌注。

### 同时引入中文 Lindera 分词

否决原因：纯质量投注，而质量不是本次驱动力。与 `lexical/tokenizer.py:3` 已记录的判断直接冲突（「this corpus is classical Chinese, where modern word segmenters mis-split constantly」），且 Lindera 中文走现代汉语词典，会把文言文的单字词粘成文中不存在的现代复合词。它还会打掉自动化验收、并把 `normalize()` 从索引与查询共用的单一入口拆成两处。归入独立实验，前提是先有评测集。

### 靠 `hnsw.iterative_scan` 解决窄过滤召回

否决原因：实测无效。`relaxed_order` 召回 0.083、`strict_order` 召回 0.017，且分别耗时 236 ms 与 171 ms，**比同条件下的精确扫描（27–67 ms）还慢 3–4 倍**。补 btree 索引才是正解。

### 直接替换，不留双实现

否决原因：新旧栈无法同时加载，验收只能退化成肉眼看结果好不好。回滚也从「翻一个环境变量」变成「重发代码 + 重建两套索引」。

## 后果

### 正面后果

- server 可运行多个写 worker，扩容路径打开；
- 发布步骤成为单事务，索引漂移这一类问题消失；
- local 单文件部署，备份即 `cp kbsvc.db`；且 WAL 下读永不阻塞，重建索引期间 API 可继续服务查询（今天因 Tantivy 目录锁必须先停服务）；
- local 窄过滤由 +31% 惩罚变为 −55% 收益；
- Tantivy 与嵌入式 Qdrant 的 Windows 绕行代码整体移除；
- payload 不再在 Qdrant 与 Tantivy 中各存一份，改为库内 join；
- `config.py` 中 `qdrant_*` 四项与 `lexical_writer_*` 三项配置消失。

### 负面后果

- **server 多一个上游供应商**。ParadeDB 是单一厂商项目，PG 安全补丁要等其重打镜像。厂商风险由双实现开关对冲，但删除旧实现后这层对冲随之消失；
- **10 万段是硬约束而非软目标**。sqlite-vec 无 ANN，严格线性；pgvector 无量化时 100 万段需约 2 GB 向量 + HNSW 图，在文档记录的 7.7 GB VM 上会很紧张。超出规模需另建 ADR；
- **`run_api_worker` 的推导要重写**。`config.py:213` 现依赖 `use_embedded_qdrant`，该属性将不存在；
- **迁移期测试矩阵翻倍**，每条检索相关的测试要在两个后端上跑，直到删除完成；
- **btree 索引缺失是静默故障**。查询不报错，只是返回空集。必须有一条测试专门断言窄过滤返回非空；
- **sqlite-vec 仍在 0.1.x**，API 稳定性与长期维护均无承诺；
- **server 侧窄过滤的方向反转，与 ADR-0005 记录的收益相抵**。ADR-0005:54 把「server profile 下按文档过滤使稠密延迟下降 32%」列为选择下推的理由之一——Qdrant Server 的 filterable HNSW 把过滤编织进图遍历，过滤越窄越快。pgvector 走的是另一条路：窄过滤时 planner 放弃 HNSW 改走 btree + 精确排序，实测 7.8 ms vs 无过滤 3.9 ms，**慢一倍**。

  绝对值上无关紧要（都是个位数毫秒），但两点必须记清：一是 ADR-0005 那条论据在迁移后不再成立，二是这条路径的代价随选择率上升而增长——窄过滤走精确扫描，命中行数越多越慢，而 filterable HNSW 没有这个特性。换来的是召回率从 0.963 升到 1.000。10 万段以内这笔交换划算，超出后需重估；
- 本 ADR 的全部实测数字来自合成夹具，真实语料的分布差异未验证。

## 验证要求

分词不变使四个格子中的三个可以写成自动化断言：

| | local | server |
|---|---|---|
| 词法 | 硬断言：top-k **集合**相等 | 硬断言：top-k **集合**相等 |
| 稠密 | 硬断言：top-k **集合**相等<br>（两侧均为精确暴力） | 软断言：见下 |

- **断言集合相等而非序列相等**：FTS5 与 Tantivy 的 tie-break 不同，序列断言会因并列误报；
- **查询集按最高文档频率分层，`df ≥ 50%` 单列为预期分歧桶**。这是实测阈值：FTS5 在此把 IDF 钳到 `1e-06`，Tantivy 给 0.11（差 5 个数量级）；低于此两引擎差异仅 1%。**该分层必须在跑 A/B 之前写进验收门**——事后追加豁免条款等于没有门；
- **server 稠密的软断言**：以精确暴力（`SET enable_indexscan = off`）为 ground truth，要求 `Recall@10(新) ≥ Recall@10(旧)`，按无过滤 / 宽过滤 / 窄过滤三档分层报告。不以旧栈输出为基准——那等于要求新栈复现 Qdrant HNSW 的近似误差；
- 查询集由语料抽样合成（正文中随机截取 8–20 字），必须覆盖带过滤的查询；
- 必须有一条测试断言窄过滤（限定单本书）返回非空，防止 btree 索引缺失的静默故障；
- 副产物：本项目首次拿到旧栈的真实召回率。

## 后续决策

以下事项不属于本 ADR，若实施需要应另建 ADR：

- **ADR-0007 的实现改打 pgvector**，并在迁移完成后进行——现在实现等于先写一遍 Qdrant 版再拆掉；
- **Lindera / 按词分词作为独立的质量实验**，前提是先有检索质量评测集（ADR-0002、0003、0007 共同指向的缺口）；
- 语料超过 10 万段后重估：sqlite-vec 的线性扫描与 pgvector 的无量化存储都会先后失效；
- `retrieval/pipeline.py:155` 的 `top_k` → `limit`：该行截断了 debug 的融合输出，挡住 RRF 权重的离线扫参。属独立缺陷，可随本迁移顺带修复，不需要 ADR；
- MinIO 是否保留。本决策不动它——单写者上限解除后多 worker 成为现实，其存在理由反而增强。

# 性能特征与调优

本文前半部分描述 `local` profile：SQLite + 本地 FS + 嵌入式 Qdrant（稠密）+ 嵌入式 Tantivy
（词法）+ API 内置 worker。基线环境为 Windows 10、Python 3.11、204 部中文古籍 / 22,659 段
（26 MB，主要为 UTF-16 txt）；它反映嵌入式模式的容量边界，不应直接外推到 server profile。

server profile 的实测见文末「server profile 实测」一节。

## 实测吞吐

| 阶段 | 观测值 |
|---|---|
| 登记（读文件 + hash + 落盘 + 入队） | 202 个文件约 60 秒 |
| 索引（解析→切分→嵌入→写入） | 约 6 篇/分钟，单 worker（*测于移除自研 BM25 之前，现应更快，未重测*） |
| 单篇平均 | 约 50 chunk（512 token 目标） |
| **检索延迟（hybrid，22,659 段，热态）** | **中位数 411ms**，其中稠密 396ms、词法 2.5ms |
| 检索延迟（dense 单路） | 中位数 394ms |
| 检索延迟（sparse 单路） | 中位数 16ms |
| 检索延迟（进程首次查询） | 多出约 7 秒，是嵌入式 Qdrant 打开集合，**每进程一次** |
| 重新嵌入（`reembed`，fastembed 512 维） | 22,659 段约 35 分钟，两路索引一起重建，不重新解析原文件（*该次测量期间有并发测试抢占磁盘，属上界*） |
| 重建词法索引（`rebuild-lexical`） | 22,659 段约 21 秒，不动稠密向量 |
| bge-small-zh-v1.5 纯嵌入吞吐 | 约 866 段/秒（CPU，batch=256） |

## 磁盘占用（22,659 段）

| 存储 | 体积 |
|---|---|
| Qdrant（稠密向量 + payload） | 179.0 MB |
| Tantivy（倒排 + stored payload） | 169.1 MB |
| SQLite（元数据 + chunk 原文） | 147.4 MB |

Tantivy 有相当一部分体积来自 stored `payload` 字段——它让「字面」单路检索无需回查另一个
存储就能渲染结果。这是用磁盘换解耦，在本地部署里划算。

> Qdrant 曾是 339.6 MB。删掉稀疏向量后集合并不会自动瘦身——必须跑一次 `reembed` 重建
> 才会真正回收，实测降到 179 MB。两路索引合计 348 MB，仍比重构前单 Qdrant 的 339.6 MB
> 略多，但换来的是 2,800 倍的词法检索速度。

## 索引阶段的成本在哪

按占比从高到低：

1. **`tokenize()` 的字符 n-gram 展开**。一部 700 KB 的书会产生上百万 token（1-gram +
   2-gram），且在纯 Python 里跑。`fastembed`/`openai` 下每段调用一次（词法索引），
   `hash` 下两次（词法 + 稠密投影）。
2. 嵌入式 Qdrant 的写入与 fsync。
3. Tantivy 的段写入与合并——单线程时占比很小，见下方「Windows 与写入线程」。

字形归一化不在这个列表里：它是 `str.translate` 走 C 实现，实测 **120 万字 65ms**，映射表
在导入时构建一次耗时 40ms。相对上面三项可以忽略。

解析和切分几乎不占时间（纯文本链路无模型）。

> 早期版本这里还有一项大头：`term_stat` 表的 upsert（单部书数万个不同 n-gram，按批写入）。
> 自研 BM25 下线后这张表已删除，索引阶段少了一次全量 tokenize 和一批 SQL 写入。

## 提速手段（按性价比排序）

**切换 server profile 后多开 worker**。本地嵌入式目录只能由 API 进程持有，不能多开本地
worker；任务表的租约机制让完整 server profile 可以直接水平扩展：
```bash
docker compose up -d --scale worker=4
```
当前 local profile 在 API 内只启动一个 worker 线程，适合单机和中小语料。

注意 Tantivy 是进程内库、持目录锁，两种 profile 都一样：**多 worker 进程不能共写同一个词法
索引目录**。server profile 下需要让词法写入集中在单一写入者后面。

**首次导入后再收紧 chunk 目标**。`KB_CHUNK_TARGET_TOKENS` 越小，chunk 越多，tokenize
总量线性上升。512 是召回与成本的平衡点。

**换 `fastembed` 未必更慢**。ONNX 批量推理（约 866 段/秒）比 Python 循环的 `hash` 投影
快得多，瓶颈会从 CPU 循环转到向量库写入。

## 检索阶段

22,659 段全量语料、fastembed 512 维、热态、n=30：

```
stage              median      min      p90      max
dense_search       396.4ms   376.9ms  1184.7ms  1211.7ms
sparse_search        2.5ms     1.4ms     2.9ms     3.9ms
total              411.4ms   391.5ms  1201.6ms  1226.3ms
```

三件事：

1. **瓶颈已经从词法转移到稠密。** 词法一路 2.5ms（Tantivy 倒排索引），稠密一路 396ms。
   `mode=sparse` 端到端只要 16ms。

2. **稠密的 396ms 几乎全是 Python 逐点处理的开销，不是数学运算。** 拆开测：

   | 环节 | 耗时 |
   |---|---|
   | 查询嵌入（ONNX bge-small-zh） | 2.3 ms |
   | 22,659×512 全量点积 + argsort（纯 numpy） | 1.23 ms |
   | **实测 `dense_search`** | **385 ms** |

   numpy 部分只占 0.3%。剩下的 380ms 在 `qdrant_client/local/local_collection.py`
   的逐点 Python 处理里。**「因为 numpy 是 C 实现所以还能接受」是错的**——这条曾经写在
   本文档里，已订正。

3. **`np.argsort` 是全量排序而非 top-k。** `local_collection.py:668` 用
   `np.argsort(scores)[::-1]`，O(N log N)；只取前 40 条用 `argpartition` 就够，
   实测 1.23ms → 0.70ms。属上游库实现，此处仅记录。

4. **p90 的 1.2 秒毛刺出现在稠密一路**，是 CPU 争用，不是索引问题。词法一路
   的 max 只有 3.9ms，非常平稳。

`store_init` + `dense_search` + `sparse_search` 精确等于 `search`——分项对不上的计时是
误导性的，有测试守着这个恒等式。融合与重排合计不到 20ms，调优空间不在那里。

`store_init` 在进程内第一次查询时约 7 秒（嵌入式 Qdrant 打开集合），**每进程一次**。常驻
服务只有第一次付这个代价，CLI 每次都付——别用 CLI 的耗时判断线上延迟。

> 历史对照：改用 Tantivy 之前，同一语料上 `sparse_search` 是 5,393ms，占 hybrid 总耗时
> 的 93%；且**每个查询改写变体各扫一遍全库**，三变体的查询要 16.4 秒。原因是嵌入式
> `qdrant-client` 对稀疏向量没有倒排索引，在 `local/sparse_distances.py` 里逐点做纯
> Python 点积。

## 嵌入式 Qdrant 没有 HNSW 意味着什么

用独立的 local Qdrant 实测（纯净集合，排除嵌入与业务代码）：

| N | 无过滤 | 带过滤 | 每点耗时 |
|---|---|---|---|
| 1,000 | 3.0 ms | 11.2 ms | 3.03 µs |
| 5,000 | 17.2 ms | 56.1 ms | 3.44 µs |
| 10,000 | 36.5 ms | 112.1 ms | 3.65 µs |

**严格线性，每点约 3.5µs。** 纯 numpy 点积每点只要 0.05µs——70 倍的差距全在 Python 开销。
有 HNSW 时是 O(log N)，下表每一档都在 1–5ms 且基本不随规模变化。

| 语料规模 | 无过滤 | 带过滤 |
|---|---|---|
| 22,659 段（当前） | ~385 ms（实测） | ~501 ms（实测） |
| 100,000 段 | ~1.4 s | ~4 s |
| 500,000 段 | ~7 s | ~20 s |

**触发点约在 10 万段**（现有语料的 4 倍）。另有隐性成本：local 模式把全部向量常驻内存，
22,659×512×4B ≈ 46MB 无所谓，100 万段就是 2GB。

### 过滤条件会让检索变慢，不是变快

`local_collection.py` 的执行顺序是**先对全部向量算分，再算过滤掩码**：

```python
590:  scores = calculate_distance(query_vector, vectors, distance)   # 全部向量
653:  mask  = self._payload_and_non_deleted_mask(query_filter, ...)  # 之后才过滤
```

所以过滤不减少计算量，反而要额外遍历全部 payload。实测：

```
无过滤（22,659 段）                385.5 ms
按 source 过滤                     501.0 ms   +30%
按单个 document 过滤（约 30 段）     505.4 ms   +31%
```

把范围收窄到 **0.13%** 的语料，反而慢 31%。**这与用户直觉相反**——界面上勾选「来源」
「限定章节」不会更快。服务端 Qdrant 有 payload 索引与过滤下推，行为正好相反。

词法一路不受影响：Tantivy 是真正的倒排索引，过滤是下推的，`mode=sparse` 端到端 16ms。

### 嵌入式相比服务端还失去了什么

| 能力 | 影响 |
|---|---|
| HNSW 索引 | O(log N) → O(N) |
| payload 索引 + 过滤下推 | 过滤从「缩小搜索空间」变成「纯加成本」 |
| 标量 / 乘积量化 | 内存占用本可降到 1/4–1/32 |
| 多进程并发 | 嵌入式持目录锁，只能一个进程 |
| 快照备份 | 只能停服务复制目录 |

## Windows 与写入线程

`KB_LEXICAL_WRITER_THREADS` 默认 **1**。Tantivy 多线程写入会并发创建/替换段文件，在
Windows 上可能与按访问扫描的安全软件抢句柄，表现为写入中途 `PermissionDenied`（`.pos` /
`.fieldnorm`）并杀掉 writer。单线程让全量重建慢约 2 倍（21 秒 vs 约 10 秒），增量写入无
影响。Linux 上，或给数据目录加了杀软排除之后，可以调高。

**批量路径走单次提交（遗留 Tantivy 后端）。** 以 Tantivy 为词法后端时，`rebuild-lexical`
与 `reembed` 都把整轮写入包在一次提交里：每批提交会让 Tantivy 在后续批次写入时并发合并
段，正是上面那个竞争的高发场景。实测逐批提交在本机有约 40% 的概率以 `os error 5` 杀掉
writer，改为单次提交后连跑 8 轮零失败。

增量导入（worker 处理单个文档）仍然逐批提交——那里文档需要尽快可检索，而且单文档的段
数量小得多。

**ADR-0008 整合后端改回逐批提交（事务化）。** 在 sqlite-vec+fts5（local）或 pgvector+
pg-search（server）后端下，不再有 Tantivy 段合并竞争，而单文件 SQLite 下一次大提交会长
时间持有写锁、阻塞作业状态写入。因此 `reembed` 改回**每批一个事务**，且每批内稠密与词法
写入共享同一个事务（`ingest/reembed.py`，ticket 08）：一批里任一步失败，这一批的两边都
回滚，不会留下「只有稠密、没有词法」的半批。嵌入调用在写事务之外（它是慢操作，不应持锁），
行在一个事务里读出、在事务外嵌入、再在第二个短事务里把两个索引一起提交。`--resume-after`
以已提交批次的最后一个 chunk id 为检查点，中断后续跑即可，不需要单独重建词法索引。
发布路径（`ingest/worker.py`）同理：元数据、向量、词法在同一个事务里提交，进程在两步之间
崩溃会整体回滚，不再退化成 dense-only 可召回。

## 当前架构与扩容信号

| 信号 | 动作 |
|---|---|
| 单 worker 追不上导入速度 | 切 PostgreSQL + Qdrant Server profile，再增加独立 worker |
| 需要 API 与 worker 同时运行 | local 默认在同一 API 进程中同时运行 |
| **chunk 数接近 10 万** | 稠密一路会到 1.4 秒（带过滤 4 秒）。切 Qdrant Server（HNSW）是唯一有效手段——嵌入式模式没有索引可调 |
| 用户抱怨「加了过滤反而更慢」 | 这是嵌入式模式的真实行为，不是错觉。见上文「过滤条件会让检索变慢」 |
| 词法检索超过 50ms | 先确认不是杀软干扰；Tantivy 在这个量级上应是个位数毫秒 |
| `lexical_docs` ≠ `chunks`（遗留 Tantivy 后端） | 两个索引漂移了，跑 `kbsvc rebuild-lexical`。ADR-0008 整合后端（sqlite-vec+fts5 / pgvector+pg-search）下发布是单事务，不应再出现这种漂移；若出现说明有 bug，应排查而非当成常规运维 |
| chunk 数超过百万级 | 使用完整 server profile、Qdrant payload 索引并独立规划容量 |
| 需要真实语义召回 | `KB_DENSE_PROVIDER=fastembed` 或 `openai`，然后 reindex |

## server profile 实测

环境：Windows 11 + Docker Desktop（WSL2），宿主 4 核 / 16 GB，VM 上限 7.7 GB。
Postgres 16 + Qdrant Server v1.18.2 + MinIO + 独立 worker 容器。fastembed
bge-small-zh-v1.5（512 维）。

语料 202 篇 / 22,350 段，与上文 22,659 段的基线**同规模，可直接对比**。测于导入完成、
HNSW 已构建、无并发写入的静态状态，n=30，热态。

**注意这台机器比 local 基线的机器弱得多**——同一个嵌入模型在这里只有 4.4 段/秒，基线机器
是约 866 段/秒。下面的稠密提速是在这个劣势下取得的。

### 检索延迟（22,350 段，n=30）

```
mode=hybrid          median      min      p90      max
store_init            0.0ms    0.0ms    0.0ms    0.0ms
dense_search         35.3ms   19.2ms   82.6ms  184.4ms
sparse_search         6.7ms    2.6ms   14.7ms   37.7ms
total                88.2ms   52.3ms  165.3ms  261.3ms

mode=dense           median      min      p90      max
dense_search         28.5ms   18.2ms   82.5ms  135.8ms
total                73.0ms   42.4ms  170.7ms  316.5ms

mode=sparse          median      min      p90      max
sparse_search         3.6ms    2.4ms    6.9ms    7.4ms
total                33.8ms   22.9ms   48.1ms   61.0ms
```

与嵌入式基线的同规模对照：

| 阶段 | 嵌入式（22,659 段） | server（22,350 段） | 变化 |
|---|---|---|---|
| `store_init` | 约 7,000 ms（每进程一次） | **0.0 ms** | 消失 |
| `dense_search`（hybrid） | 396 ms | **35.3 ms** | **11.2×** |
| `total`（hybrid） | 411 ms | **88.2 ms** | **4.7×** |
| `sparse_search` | 2.5 ms | 6.7 ms | 慢 2.7×，见下 |

`sparse_search` 变慢是机器差异，不是回退：Tantivy 一路完全不经过 Qdrant，两种 profile 的
代码路径相同。同理 `mode=sparse` 端到端从 16ms 变成 33.8ms——那里面的改写与重排要跑查询
嵌入，而这台机器的 ONNX 推理慢得多。

三点结论：

1. **`store_init` 归零。** 嵌入式模式下进程内首次查询要多付约 7 秒打开集合，每进程一次；
   连到 Qdrant Server 之后这项消失。受益最大的是 CLI 和短生命周期进程——上文「别用 CLI
   的耗时判断线上延迟」那条警告在 server profile 下不再适用。

2. **稠密一路不再是纯 Python 逐点扫描。** 同规模下 396ms → 35.3ms。嵌入式模式是严格线性的
   O(N)（每点约 3.5µs），服务端是 HNSW 的 O(log N)——真正的差别不在这 11 倍，而在斜率：
   语料再翻几倍，嵌入式按比例劣化，服务端基本持平。

   HNSW 的构建有阈值。本次实测 `indexing_threshold=10000`，语料只有 8,980 段时
   `indexed_vectors_count` 是 0，走的仍是精确检索（只不过实现在 Rust 里而非 Python）；
   满库 22,350 段后为 20,367，图已建成。**规模小于阈值时看不到 HNSW 的收益，别据此判断
   服务端没用。**

3. **`total` 与两路之和的差额主要是查询嵌入。** 上文 local 基线记录「融合与重排合计不到
   20ms」，这里差额约 46ms。原因不在融合或重排，而是这台机器的 ONNX 推理慢得多——见下节。

### 过滤条件：行为与嵌入式模式相反

同一查询，`mode=dense`，22,350 段，n=20：

| 过滤 | dense_search 中位数 | server | 嵌入式（同规模） |
|---|---|---|---|
| 无 | 40.4 ms | — | — |
| 按 source | 26.9 ms | **−33%** | +30% |
| 按单个 document | 27.5 ms | **−32%** | +31% |

嵌入式是先对全部向量算分、再算过滤掩码，所以过滤只增不减；服务端有 payload 索引与过滤
下推，范围越窄越快，与用户直觉一致。

**这条结论与语料规模无关**，是切换到 Qdrant Server 后最容易验证的结构性变化：界面上勾选
「来源」「限定章节」从纯加成本变成了真正的加速。

### 嵌入吞吐是这台机器的真实瓶颈

实测 bge-small-zh-v1.5 在容器内（与 worker 争抢 CPU）：

| batch | 吞吐 |
|---|---|
| 32 | 4.4 段/秒 |
| 128 | 3.5 段/秒 |

local 基线机器上是约 866 段/秒——相差约 200 倍。由此：

- 导入全量实测：**202 篇 / 22,350 段 / 160.5 分钟**，即约 **1.26 篇/分钟、2.3 段/秒**
  （平均 110.6 段/篇），与上面 4.4 段/秒的嵌入上限同量级——差额是解析、切分与两个索引的
  写入。上文「索引阶段成本以 `tokenize()` 的 n-gram 展开为首」是 local 基线机器上的结论，
  **在这台机器上不成立**：瓶颈是 ONNX 推理。
- **加大 `KB_DENSE_BATCH_SIZE` 在 CPU 已饱和时是负优化**（batch=128 反而更慢）。这个旋钮
  只在还有空闲核心时有用。
- 查询嵌入同样受影响，这是 `total` 与两路之和差额的来源。

结论：server profile 解决的是**检索**的扩展性（HNSW、过滤下推、多进程并发、无 store_init），
**导入吞吐仍然取决于嵌入算力**。要提高导入速度，方向是更快的 CPU、GPU，或把
`KB_DENSE_PROVIDER` 指向外部推理服务（vLLM / TEI），而不是加 worker 副本——见下节。

### worker 副本数的硬上限是 1

任务表的租约机制支持水平扩展，但 Tantivy 是进程内库并持目录独占锁，而 worker 在
`ingest/worker.py` 里内联写词法索引。**第二个 worker 副本抢不到 writer 会失败**，
`docker compose up -d --scale worker=4` 今天不可用。

上文「切 server profile 后多开 worker」需要按此修正：那句话描述的是任务队列的能力，不是
当前部署的能力。真正扩 worker 需要先把词法写入收敛到单一写入者背后。

### 共享索引目录与读侧可见性

api / worker / mcp 共享一个卷承载词法索引。只读进程按 1 秒节流调用 `Index.reload()`
（`lexical/tantivy_store.py` 的 `_refresh_reader`）来看见 worker 的提交，否则它们会一直停在
启动那一刻的段集合上。代价是每秒至多一次段元数据重读，实测 `sparse_search` 中位数 12.5ms
（含与导入的争抢），未见可归因于 reload 的开销。

## 抄袭检测的性能约束

仅 PostgreSQL。见 [ADR-0001](adr/0001-selectively-port-noplag-into-kbsvc.md)。

### 两类成本，性质完全不同

| 阶段 | 何时发生 | 成本主体 |
|---|---|---|
| 投影构建 | 入库后异步、或回填时 | pysbd 分句 + winnowing 指纹，CPU 密集 |
| 检测执行 | 查询时 | GIN 候选检索 + seed-extend 对齐 |

构建是一次性的、可中断的、可离线跑的；检测在请求路径上，受预算约束。

**分句是构建的主要成本，不是指纹。** pysbd 在有自然段落结构的文档上接近线性，
但在没有段落边界的长连续文本上退化得厉害——本语料的古籍 txt 常常正是这种形状。

### 默认限额

| 参数 | 默认 | 说明 |
|---|---|---|
| `KB_PLAG_MAX_INPUT_CHARS` | 500,000 | 硬上限，超过返回 413 |
| `KB_PLAG_BUDGET_SECONDS` | 60 | 参考预算 |
| `KB_PLAG_BUDGET_REFERENCE_CHARS` | 100,000 | 上面那 60 秒买的字符数 |
| `KB_PLAG_MAX_ACTIVE_CHECKS_PER_KEY` | 2 | 每凭据并发 |
| `KB_PLAG_CANDIDATE_TOP_K` | 50 | 每个查询分块的候选上限 |

预算**按输入规模缩放**：配置的秒数买的是参考字符数，更长的输入按比例获得更长
时间。固定预算会让大文档必然超时而小文档永远用不完。

预算耗尽以 `completed_partial` 收尾并带 `coverage_reason`，**不是失败**——
但也绝不能被读成「没有抄袭」。

### GIN 索引是承重的

候选检索是 `fingerprints && :probe`，**没有 Python 扫描兜底**（ADR-0001 有意如此）。
后果是：索引失效的表现是无上限地变慢，而不是报错——最难被发现的失败模式。

因此：

- `kbsvc plagiarism rebuild-df` 会顺带执行 `ANALYZE plag_corpus_chunk`。
  回填期间该表大小变化几个数量级，**过期的行数估计正是让规划器放弃 GIN 的原因**；
- 有一项测试用执行计划断言代表性查询确实命中索引名；
- `/readyz` 单独检查 GIN 索引是否存在。

### 停用词过滤

高频指纹（套语、格式化开头，本语料里是各类术数文本共有的固定表述）会让 `&&`
探针扫过语料的一大片而毫无区分度。`KB_PLAG_DF_RATIO_THRESHOLD`（默认 0.25）
按**文档频率占比**过滤。

DF 表为空时过滤失效，召回质量会明显下降——`rebuild-df` 是回填后的必需步骤，
不是可选优化。语料少于 3 篇时不做过滤：那时每个指纹都「出现在大多数文档里」。

### 存储量级

`plag_corpus_chunk` 是主要占用：每个分块存一份文本副本加一个 `BIGINT[]` 指纹数组。
指纹数量约为归一化后字符数的 1/w（默认 w=8），但滑窗重叠（默认 4 句窗、1 句重叠）
意味着同一段文本会被存约 4/3 次。

规划容量时按**原文的数倍**估，而不是按原文估。

### 报告来源定位的成本

“到书里看”只在现有 `document_chunks` 上按版本和字符区间做有界窗口查询，默认返回
前后各 2 个 chunk，最多扫描 200 个 chunk；它不重新跑查重、不访问 Qdrant/Tantivy，
也不创建或重建索引。窗口查询按 `(version_id, ordinal)` 与字符区间过滤，异常大的
区间会返回 `passage_location_unavailable`，避免一次请求加载整本书。

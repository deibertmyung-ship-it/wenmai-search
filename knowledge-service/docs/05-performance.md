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

### ADR-0008 A/B 验收：新旧检索栈对比（真实语料，2026-08-18）

这是本项目第一次拿到旧栈（Qdrant Server + Tantivy）在真实语料上的召回率数字——此前从无
评测集（ADR-0002/0003/0007 共同指向的缺口），全靠 A/B 框架跑真实数据才第一次量出来。

语料：208 篇 / 22,389 段（与上文 202 篇 / 22,350 段同一批语料，计数口径略有差异，量级一致）。
方法：真实数据安全复制（`pg_dump`/`pg_restore`，不改动生产库）到独立 ParadeDB 实例，真实
`migrate-vectors`（Qdrant → pgvector，22,389 点，389.1s，抽样 100 点逐位比对全部一致）+
真实 `rebuild-lexical`（→ pg_search），四个真实 store（Qdrant/Tantivy 旧栈，pgvector/
pg_search 新栈）同进程对比，1000 条合成查询集（`kbsvc ab-compare --profile server`）。

```
硬断言   server 词法   21/53 通过  (df<50% 桶)
                       668 条分歧       (df≥50% 桶，预期内)
软断言   server 稠密   Recall@10  旧 0.8801 → 新 0.9571   （Δ均值 +0.0772）
                       无过滤: 991 查询，回退 38 条
                       窄过滤: 1 查询，回退 0 条
                       宽过滤: 1 查询，回退 0 条
```

**稠密向量（pgvector）质量已明显反超 Qdrant**（0.9571 对 0.8801）——但这是调过
`hnsw.ef_search`（默认 40 → 200，见上文「pgvector HNSW 参数」相关记录）之后的结果；未调参
前默认值下新栈是落后旧栈的（约 0.76–0.82），足以说明这一参数此前从未针对真实规模数据验证过。

**词法（pg_search）打分修复后大幅改善但未达票面「集合完全相等」的硬指标**：从最初 3/53
（一个把全文检索误用「精确值过滤」原语 `term_set` 导致打分完全失真的 bug，已修，见
issue tracker 07 号票）提升到 21/53。对全部 32 条未通过的低频桶查询逐条复核（同一 seed=42，
与正式跑同一批查询，非重新抽样）：top-10 重合度全部 ≥ 70%（9/10 重合 22 条、8/10 重合 7 条、
7/10 重合 3 条），没有一条低重合或零重合——不是结构性错误，是 top-10 边界的排名互换，
看起来是 pg_search 与 Tantivy 两个独立 BM25 实现间正常的数值差异（同源不同码），不是分词或
过滤逻辑失效。**这条 32/32 全部可归因，满足票据 11 的红线要求**（无法归因的分歧为零）。

**这个结果按票面标准判定是 FAIL**（票面要求硬断言 100% 通过、软断言零回退，不允许为了
让结果好看而放宽阈值）——但「新栈整体检索质量是否优于旧栈」和「是否满足这条写死的验收线」
是两个不同的问题：前者的答案是「是」（稠密更好，词法从灾难性提升到大部分一致），后者的答案
是「否」。是否要因此调整验收线本身（比如词法格改成软断言、或允许一定比例的边界分歧），还是
维持严格标准、继续在打分细节上追平，是产品/架构层面的判断，不在这份实测记录的范围内。

**local profile（sqlite-vec + FTS5 vs. 嵌入式 Qdrant + Tantivy）同一批真实语料的结果**：

```
硬断言   local lexical  442/993 通过  (df<50% 桶 31/53，df≥50% 桶 411/940 预期内)
硬断言   local dense    992/993 通过  (top-k 集合完全相等)
```

两个词法桶的低频段分歧（22 条）逐条核实全部可归因（重合度 9/10 或 8/10，无一低于 8/10），
比 server 侧还干净。稠密 993 条里唯一 1 条分歧也已定位：不是空结果探针一类边界查询，是
旧栈（嵌入式 Qdrant，HNSW 近似搜索）与新栈（sqlite-vec，本文档上一节实测确认的严格线性
暴力扫描）在候选相似度接近时于 top-10 最后一名产生的边界差异——比较一个近似索引与一个
精确索引，这类边界漂移是检索方式的内在原理决定的，不是实现问题，理论上无法在维持两种索引
策略不变的前提下消除到真正的 0。完整归因过程见
`.scratch/storage-consolidation/11-ab-report.md`。

### ADR-0008 延迟基准：新旧栈各半侧对比（真实语料，2026-08-19）

A/B 框架只测召回/等价性，不测延迟。本节补齐票据 11 的欠账。方法：复用 `ab_compare`
的 `load_stacks` / `generate_queries`，四个真实 store 同进程存活，同一批合成查询的
查询向量只嵌入一次并**排除在计时外**（两栈共享同一个 embedder，嵌入不是存储层差异）。
275 条无过滤随机查询 + 80 条窄过滤（单 document）+ 40 条宽过滤（单 source），前 25 条
预热不计入，热态。语料同上（208 篇 / 22,389 段）。

**跑在安全副本上，未碰生产库。** 新栈连 `kbsvc-server-realdata`（pg_restore 副本，
端口 55434）；旧栈稠密连真实 `kbsvc-qdrant-1` 只读，词法用从 docker 卷 `kbsvc_kbdata`
只读复制出来的 Tantivy 索引。原始输出见
`.scratch/storage-consolidation/bench_latency_server_300.log`，脚本
`.scratch/storage-consolidation/bench_latency.py`。

| 半侧 / 过滤 | 旧栈 median | 旧栈 p95 | 新栈 median | 新栈 p95 | median 新/旧 |
|---|---|---|---|---|---|
| 稠密 / 无过滤 | 64.0 ms | 149.2 ms | **45.8 ms** | 107.5 ms | **0.71×（更快）** |
| 稠密 / 窄（单 document，n=80） | 60.6 ms | 138.3 ms | **47.9 ms** | 188.4 ms | 0.79× |
| 稠密 / 宽（单 source，n=40） | 72.5 ms | 193.0 ms | **51.6 ms** | 121.1 ms | 0.71× |
| 词法 / 无过滤 | **13.3 ms** | 28.0 ms | 78.8 ms | 191.0 ms | 5.92×（见下节修正） |

参照：embedder（bge-small-zh-v1.5，fastembed CPU）自身 median 14.2 ms / p95 37.6 ms，
与存储无关、两栈都要付。

> **绝对值只在本机内部可比，不要外推。** 测量主机是 Intel i5-5300U（2015 年双物理核
> 移动 CPU，4 线程），Postgres 跑在 Docker Desktop 的 WSL2 虚拟机里，Tantivy 跑在
> Windows 原生进程内。容器里 `SELECT count(*) FROM generate_series(1,2e7)` 要 24–40 秒
> （常见服务器约 2 秒）。这些数字对容量规划没有参考价值，只有**同一次循环里交叉测出的
> 比值**是可信的。下节的复核也说明：本机后台负载会让同一份代码的比值在 4.08× 到 7.45×
> 之间浮动。

**稠密一路变快**：pgvector HNSW（`hnsw.ef_search=200`）在三档过滤上 median 都比 Qdrant
Server 低约 20–30%，无过滤 p95 也更低（108 vs 149 ms）。窄过滤 p95 有一条 551 ms 的
离群（pgvector 那次），median 不受影响——观察期需留意窄过滤尾延迟，但样本里仅 1/80。

**词法一路变慢，是本次切换唯一明确的性能回退**，但上表那个 5.92× **不是 pg_search 的
固有性能**——见下节的根因复核。量级始终在「亚秒、可交互」范围。一个与配置无关、会放大
影响的结构性事实：混合检索目前两路是**串行**执行（见 `retrieval/pipeline.py` 的
`_run_retrievers`：dense 段跑完才进 sparse 段，没有用线程/协程并行），所以词法的增量直接
叠加到 hybrid 端到端，而不是被稠密一路掩盖。若改成并行，hybrid 端到端由较慢的一路决定，
词法回退的影响会从「相加」变成「封顶」——这是观察期内一个低风险、高收益的优化项。

### ADR-0008 词法回退的根因复核（2026-08-20）

上节的 5.92× 是黑盒单值。复核把 pg_search 路径拆成分层计时 + 同环境交叉实验，结论是
**这个数字测的是一份可修复的配置，不是引擎能力**。脚本在
`.scratch/storage-consolidation/diag_*.py`，全部只跑安全副本，生产库未碰。

先说**排除**掉的假设，避免以后重复排查：top-N 已正确下推进索引
（`Exec Method: TopKScanExecState`、`Heap Fetches: 10`，不是全量物化再排序）；无磁盘 I/O
（`Buffers: shared hit=460`，bm25 索引 77 MB 远小于 `shared_buffers` 1974 MB）；连接池确实
在复用（30 次 checkout 拿到同一个 backend pid，`engine.connect()` 仅约 2 ms）；jsonb /
TOAST 取回无影响（`SELECT` 去掉 `lexical_payload` 后执行时间不变）；也不是缓存预热——把
两栈顺序随机化并分离「首次触碰 / 重复」后差距反而更大。段数假设方向还测反了：旧栈
Tantivy 的真实索引是 **6 段**，比 pg_search 的 4 段还多。

真正成立的是三项，安静主机上同一循环交叉测得（100 条随机查询，n=291 的等价性覆盖
`generate_queries` 的全部类别）：

| # | 差异 | 延迟 | 对检索结果的影响 |
|---|---|---|---|
| — | Tantivy（旧栈基准） | 10.9 ms | — |
| — | **现状**：活 `chunk` 4 段 + `record: position` + 19×`term` | **44.4 ms（4.08×）** | — |
| f1 | 段合并 4 → 1（重建索引） | −1.5× | **不中性**：287/291 集合相同、282/291 顺序相同 |
| f3 | `body` 的 `record: position` → `freq` | −1.1× | **完全中性**：291/291 集合与顺序全同 |
| f2 | 单个 `paradedb.match` 替代 19 个 `paradedb.term` | −1.5× | **完全中性**：291/291 集合与顺序全同 |
| — | **三项全修** | **19.7 ms（1.82×）** | — |

**f3 `record` 选项是纯浪费。** `paradedb.schema('chunk_bm25')` 显示 `body` 是 ParadeDB 的
默认 `position`，而 `TantivyLexicalStore` 用的是 `index_option="freq"`。`build_query` 从不
发短语查询，位置信息一次也用不到，代价是索引大一倍（57 MB → 28 MB）、建索引慢四倍
（66 s → 16 s）。改 `pg_search_ddl.py` 的 `_TEXT_FIELDS_CONFIG` 即可，需重建索引。

**f2 的子句形状。** `_body_should` 为每个词元发一个 `paradedb.term`，中文 unigram+bigram
下每查询中位 19 个、最多 32 个；planning 时间随子句数近似线性增长（1 个词 2.5 ms，
40 个词 70 ms），执行时间也一样。`paradedb.match` 用一次函数调用表达同一个「任一词元命中、
按真实 BM25 打分」的并集。注意 `_body_should` 的 docstring 记着 `term_set` 曾「看起来等价」
却毁了排序，所以这里是按真实 id 逐类别验的，不是假设的。

**f1 段合并不是结果中性的，这点必须单独强调。** Tantivy 的 IDF 按段局部统计，段数变了打平
附近的排序就会动——291 条里 4 条 top-10 集合变化、9 条顺序变化。这意味着对生产做
`REINDEX INDEX CONCURRENTLY chunk_bm25` 会轻微改变少数查询的结果，属于要人判的动作，不是
纯运维优化。（同理，旧栈 6 段与新栈 4 段本来也不在同一套 IDF 上打分，这是既有性质，不是本次
迁移引入的。）

**保留的观察期 runbook。** 这个副本最初测出 pg_search 慢到 ~1500 ms（63× Tantivy），
`EXPLAIN ANALYZE` 显示 `Segment Count: 10`——它经 `pg_restore` 物理复制而来、段从未合并。
`REINDEX` 后（10 → 4 段）降到 ~79 ms。所以线上词法延迟若异常飙到秒级，**先查 `EXPLAIN` 里的
`Segment Count`**。但要注意本次复核的修正：一次 `REINDEX` 只把段数压到 4，重建才到 1，两者
之间还有约 1.5× 的差距。

### ADR-0008 后，「嵌入式 Qdrant 外推到 10 万段」一节的去向

上文「嵌入式 Qdrant 没有 HNSW 意味着什么」中按 N 外推到 10 万 / 50 万段的那张表
（~1.4 s / ~7 s）描述的是**迁移前 local profile 的嵌入式 Qdrant**——纯 Python 逐点
O(N) 扫描。迁移后：

- local profile 的稠密一路换成 sqlite-vec，仍是精确暴力 KNN（`spike_01_sqlite_vec.py`
  已确认严格线性、无 ANN），斜率特征与旧嵌入式 Qdrant 同类，但 KNN 在 sqlite-vec 的
  Rust 扩展里执行，而非旧的 `local/sparse_distances.py` 纯 Python 逐点点积；
- **server profile 的稠密一路是 pgvector HNSW，O(log N)，不再随语料规模线性劣化**，
  「10 万段触发 1.4 秒」这个预警对 server profile 不再成立。扩容信号表中
  「chunk 数接近 10 万 → 稠密 1.4 秒」一条仅对旧嵌入式 local 栈有效，该栈在票据 13
  删除旧实现后随之移除。

local profile 端到端（含嵌入/融合/重排，而非 spike 的净向量基准）本次未重测——票据 11
用的 local 真实语料副本（SQLite + 嵌入式 Qdrant 副本）在复测前已被清理，重建需
~10 分钟向量迁移外加嵌入式 Qdrant 冷缓存建 HNSW 图。server 是本次实际切换的目标 profile，
延迟基准以上表为准；local 端到端数字留待有需要时补测。

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

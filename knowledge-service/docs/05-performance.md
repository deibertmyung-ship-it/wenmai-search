# 性能特征与调优

当前 `local` profile 使用 SQLite + 本地 FS + 嵌入式 Qdrant（稠密）+ 嵌入式 Tantivy（词法）
+ API 内置 worker。下列基线环境为 Windows 10、Python 3.11、204 部中文古籍 / 22,659 段
（26 MB，主要为 UTF-16 txt）；它反映嵌入式模式的容量边界，不应直接外推到 server profile。

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

2. **稠密的 396ms 是嵌入式 Qdrant 的全量扫描**：local 模式的 `qdrant-client` 用 numpy
   对全部 22,659 个向量算距离，没有 HNSW。它之所以还能接受，只是因为 numpy 是 C 实现。
   语料继续增长时这一路是下一个瓶颈，**切 Qdrant Server（有真正的 HNSW 索引）是主要手段**。

3. **p90 的 1.2 秒毛刺出现在稠密一路**，是 ONNX 推理与 CPU 争用，不是索引问题。词法一路
   的 max 只有 3.9ms，非常平稳。

`store_init` + `dense_search` + `sparse_search` 精确等于 `search`——分项对不上的计时是
误导性的，有测试守着这个恒等式。融合与重排合计不到 20ms，调优空间不在那里。

`store_init` 在进程内第一次查询时约 7 秒（嵌入式 Qdrant 打开集合），**每进程一次**。常驻
服务只有第一次付这个代价，CLI 每次都付——别用 CLI 的耗时判断线上延迟。

> 历史对照：改用 Tantivy 之前，同一语料上 `sparse_search` 是 5,393ms，占 hybrid 总耗时
> 的 93%；且**每个查询改写变体各扫一遍全库**，三变体的查询要 16.4 秒。原因是嵌入式
> `qdrant-client` 对稀疏向量没有倒排索引，在 `local/sparse_distances.py` 里逐点做纯
> Python 点积。

## Windows 与写入线程

`KB_LEXICAL_WRITER_THREADS` 默认 **1**。Tantivy 多线程写入会并发创建/替换段文件，在
Windows 上可能与按访问扫描的安全软件抢句柄，表现为写入中途 `PermissionDenied`（`.pos` /
`.fieldnorm`）并杀掉 writer。单线程让全量重建慢约 2 倍（21 秒 vs 约 10 秒），增量写入无
影响。Linux 上，或给数据目录加了杀软排除之后，可以调高。

**批量路径走单次提交。** `rebuild-lexical` 与 `reembed` 都把整轮写入包在一次提交里：每批
提交会让 Tantivy 在后续批次写入时并发合并段，正是上面那个竞争的高发场景。实测逐批提交在
本机有约 40% 的概率以 `os error 5` 杀掉 writer，改为单次提交后连跑 8 轮零失败。

增量导入（worker 处理单个文档）仍然逐批提交——那里文档需要尽快可检索，而且单文档的段
数量小得多。

代价：`reembed` 中途被打断时，词法索引会停留在上一次提交的状态。稠密一路可以用
`--resume-after` 续跑，词法一路直接 `rebuild-lexical` 重建即可（约 21 秒），不值得为它
牺牲写入稳定性。

## 当前架构与扩容信号

| 信号 | 动作 |
|---|---|
| 单 worker 追不上导入速度 | 切 PostgreSQL + Qdrant Server profile，再增加独立 worker |
| 需要 API 与 worker 同时运行 | local 默认在同一 API 进程中同时运行 |
| **稠密检索超过 1 秒** | 切 Qdrant Server（HNSW）；嵌入式模式是全量扫描，没有索引可调 |
| 词法检索超过 50ms | 先确认不是杀软干扰；Tantivy 在这个量级上应是个位数毫秒 |
| `lexical_docs` ≠ `chunks` | 两个索引漂移了，跑 `kbsvc rebuild-lexical` |
| chunk 数超过百万级 | 使用完整 server profile、Qdrant payload 索引并独立规划容量 |
| 需要真实语义召回 | `KB_DENSE_PROVIDER=fastembed` 或 `openai`，然后 reindex |

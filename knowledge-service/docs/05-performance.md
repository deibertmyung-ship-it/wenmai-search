# 性能特征与调优

数据来自本机实测：Windows 10、Python 3.11、`local` profile（SQLite + 本地 FS + 嵌入式
Qdrant）、`hash` 稠密嵌入、语料为 `book/` 下 202 部中文古籍（26 MB，主要为 UTF-16 txt）。

## 实测吞吐

| 阶段 | 观测值 |
|---|---|
| 登记（读文件 + hash + 落盘 + 入队） | 202 个文件约 60 秒 |
| 索引（解析→切分→嵌入→写入） | 约 6 篇/分钟，单 worker |
| 单篇平均 | 约 50 chunk（512 token 目标） |
| 检索延迟（hybrid，22350 段，热态） | **约 5.8 秒**，其中稀疏占 5.4 秒 — 见下方「检索阶段」 |
| 检索延迟（同上，进程首次查询） | 约 13.4 秒，多出的 7.4 秒是嵌入式 Qdrant 打开集合 |
| 重新嵌入（`reembed`，fastembed 512 维） | 22350 段约 5 分钟，不重新解析原文件 |
| bge-small-zh-v1.5 纯嵌入吞吐 | 约 866 段/秒（CPU，batch=256） |

## 索引阶段的成本在哪

按占比从高到低：

1. **`tokenize()` 的字符 n-gram 展开**。一部 700 KB 的书会产生上百万 token（1-gram +
   2-gram），且在纯 Python 里跑。它被调用三次：BM25 词表统计、稀疏文档向量、`hash`
   稠密向量。
2. **`term_stat` 的 upsert**。单部书有数万个不同 n-gram，按批写入。
3. 嵌入式 Qdrant 的写入与 fsync。

解析和切分几乎不占时间（纯文本链路无模型）。

## 提速手段（按性价比排序）

**多开 worker**（server profile）。任务表用租约抢占，worker 之间不会重复处理同一任务，
线性扩展：
```bash
docker compose up -d --scale worker=4
```
`local` profile 做不到——嵌入式 Qdrant 对目录持独占锁，同一 `KB_DATA_DIR` 只能有一个进程。

**放大 SQL 批大小**。`db/repo.py` 的 `_SQL_VAR_BATCH = 500` 是保守值（兼容老 SQLite 的
999 参数上限）。现代 SQLite 与 PostgreSQL 都支持 32766，调到 5000 可显著减少语句数。

**首次导入后再收紧 chunk 目标**。`KB_CHUNK_TARGET_TOKENS` 越小，chunk 越多，三处
tokenize 的总量线性上升。512 是召回与成本的平衡点。

**换 `fastembed` 未必更慢**。ONNX 批量推理（约 866 段/秒）比 Python 循环的 `hash` 投影
快得多，瓶颈会从 CPU 循环转到向量库写入。`reembed --batch-size 256` 实测 22350 段约 5 分钟，
其中嵌入只占约 30 秒，其余是嵌入式 Qdrant 的 upsert。

## 检索阶段

在 22350 段的全量语料上实测（`local` profile，嵌入式 Qdrant，fastembed 512 维）：

```json
// 进程内第一次查询
{"rewrite": 0.03, "search": 13352, "store_init": 7432, "dense_search": 557,
 "sparse_search": 5363, "fuse": 0.09, "rerank": 6.1, "total": 13358}

// 同一进程内后续查询
{"store_init": 0, "dense_search": 362, "sparse_search": 5393, "total": 5762}
```

两件事：

1. **`store_init` 7.4 秒**是嵌入式 Qdrant 打开 310MB 集合，**每进程一次**。常驻服务里只有
   第一次查询付这个代价，CLI 每次都付——所以别用 CLI 的耗时判断线上延迟。
2. 热态下**稀疏检索占 93%**（5.4s / 5.8s）。 原因是嵌入式 Qdrant 对稀疏向量做全量扫描，而中文查询经
字符 1/2-gram 展开后有几十个 term，每个都要扫全库。Qdrant 服务端对稀疏向量建**倒排索引**，
这一项会降到几十毫秒——这是切 server profile 最直接的收益，比多开 worker 更急迫。

在此之前的临时缓解：
- `mode=dense` 单路检索只要约 350ms
- 调小 `KB_RETRIEVAL_OVERFETCH`（默认 4）
- 语料控制在 2 万段以内（Qdrant 自己在超过 20000 点时就会告警）

`store_init` + `dense_search` + `sparse_search` 精确等于 `search`——分项对不上的计时是
误导性的，有测试守着这个恒等式。融合与重排合计不到 10ms，调优空间不在那里。

## 何时该换 Profile

| 信号 | 动作 |
|---|---|
| 单 worker 追不上导入速度 | 切 server profile，扩 worker |
| 需要 API 与 worker 同时运行 | 切 server profile（嵌入式 Qdrant 独占锁） |
| **稀疏检索超过 1 秒** | Qdrant 服务模式——倒排索引，这是最先撞到的墙（约 2 万段） |
| chunk 数超过百万级 | Qdrant 服务模式 + payload 索引（嵌入式模式索引无效） |
| 需要真实语义召回 | `KB_DENSE_PROVIDER=fastembed` 或 `openai`，然后 reindex |

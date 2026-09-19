# ADR-0008 观察期记录（票据 12）

- 日期：2026-09-19
- 分支：`docs/adr-0008-storage-consolidation`
- 工单：`.scratch/storage-consolidation/issues/12-production-cutover.md`
- 对照：`docs/2026-08-19-session-pg-cutover.md`

本次只记录能核验的事实。没有启动任何已停止的生产容器，也没有改检索代码。

## 三条验收

| 验收项 | 结论 |
|---|---|
| 生产运行新后端满 14 天无回退 | **不成立。** 日历已过 31 天，但进程实际只跑了约 18 小时（API/worker）到约 2 天（Postgres），之后整栈停机。停机前配置仍是新后端，未见翻回 qdrant/tantivy。 |
| 观察期指标记录在案 | **部分成立。** 没有热态生产 P95 / 摄入吞吐 / 磁盘的连续观察。延迟与召回基线仍以 2026-08-19 切之前在安全副本上测的数字为准，见 `knowledge-service/docs/05-performance.md`。 |
| 多 worker 吞吐已实测 | **不成立。** `KB_WORKER_REPLICAS` 默认仍为 1；2026-08-20 的单 worker 实验被中途杀掉，没有数字。 |

因此票据 12 **不能关单**。ADR-0008 删除旧实现（票据 13）的硬触发条件仍未满足。

## 容器时间线（Docker inspect，UTC）

| 容器 | 创建 | 最后启动 | 停止 | 退出码 |
|---|---|---|---|---|
| `kbsvc-api-1` | 2026-08-19 06:07 | 2026-08-19 06:08 | 2026-08-20 00:13 | 255 |
| `kbsvc-worker-1` | 2026-08-19 06:07 | 2026-08-19 06:08 | 2026-08-20 00:13 | 255 |
| `kbsvc-postgres-1` | 2026-08-18 09:38 | 2026-08-20 02:13 | 2026-08-21 07:10 | 255 |

- 退出码 255 且多个容器同一时刻停下，符合 Docker Desktop / WSL 被硬停，不是应用自己崩溃、也不是一次配置回滚。
- `kbsvc-postgres-1` 容器 ID 仍是切生产时记下的 `7c5ba7099de1`，镜像仍是钉死的 ParadeDB digest `sha256:dca046b8a0ddd070c368d5254cbb7f596b3b7bc67bc2efa7c0fbe881087883bb`。
- 2026-09-19 复查时这些容器全部是 `Exited`，状态文案为 “4 weeks ago”。同机 Docker 里在跑的是别的项目，不是文脉。

## 停机前的后端（未回退）

从已停止的 `kbsvc-api-1` / `kbsvc-worker-1` 环境变量读取（不是猜测）：

- `KB_PROFILE=server`
- `KB_VECTOR_BACKEND=pgvector`
- `KB_LEXICAL_BACKEND=pg-search`

`kbsvc-qdrant-1` 容器还在（同样已停），符合「回滚目标保留、切生产后不再承担检索」的设计。没有证据表明检索后端曾被翻回 `qdrant` / `tantivy`。

## 指标：有什么、缺什么

已有、且已写入 `knowledge-service/docs/05-performance.md` 的（切生产前，安全副本，不是观察期热态）：

- 稠密半侧 median 约为旧栈的 0.71–0.79×（更快）。
- 词法半侧在 f1/f2/f3 优化前明显慢于 Tantivy；优化后副本上约 1.82×。
- 窄过滤 p95 有过 551 ms 离群（80 次里 1 次）。

观察期要求盯、但本次取不到的：

- 标题级 / 限定书名查询在生产 HTTP 上的持续表现（栈已停）。
- 生产 P95。
- 摄入吞吐（worker 副本始终为 1）。
- 切生产之后磁盘占用的变化。

## 多 worker

`deploy/docker-compose.yml` 仍是 `replicas: ${KB_WORKER_REPLICAS:-1}`，注释写明要等本票吞吐验证。2026-08-20 handoff 记录过一次针对副本库的单 worker 摄入，约 49 分钟后被环境杀掉，没有吞吐数字，也没有双 worker 对照。

这是 ADR-0008 的核心承诺（解开 Tantivy 单写者锁）。在补测之前不应把 12 号票标成 done，也不应开始删旧实现。

## 若要补完 12 号票

1. 明确授权后重新拉起 `kbsvc-postgres-1` / api / worker（不要 `docker volume prune`，旧 PG16 卷和 Qdrant 数据仍是回滚资产）。
2. 连续跑满 14 天，记录限定书名查询、P95、磁盘。
3. 把 `KB_WORKER_REPLICAS` 提到 ≥2，用同一批语料对照单 worker 摄入吞吐。
4. 人确认无回退后再关 12、开 13。

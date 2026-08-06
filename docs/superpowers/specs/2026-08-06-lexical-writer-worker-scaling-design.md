# 设计：解除 worker 副本上限（专职 lexical-writer）

- 日期：2026-08-06
- 状态：待实现
- 相关：`knowledge-service/docs/04-runbook.md` 第 2 节、`docs/05-performance.md`「worker 副本数的硬上限是 1」

## 背景

server profile 下 `KB_WORKER_REPLICAS` 只能是 1。原因不是任务队列——`ingest_job` 的租约机制
本来就支持多消费者——而是 **Tantivy 是进程内库并持目录独占锁**，而 worker 在
`ingest/worker.py` 里内联写词法索引。第二个 worker 副本拿不到 writer 会失败。

`README.md` 与 `05-performance.md` 曾把「可水平扩展」写成当前能力，已于 2026-08-06 订正。

## 目标

让 `docker compose up -d --scale worker=N` 成为合法操作。

**这是正确性目标，不是性能目标。** 实测本机瓶颈是嵌入算力（4.4 段/秒），单机加副本不会更快；
本设计只拆掉架构天花板，使换硬件或改用外部推理服务（vLLM / TEI）时能直接受益。

## 非目标

- 不提高单机导入吞吐
- 不改变检索行为、评分或召回质量
- 不替换 Tantivy
- 不引入 alembic

## 约束

1. **零 schema 变更。** 项目只用 `Base.metadata.create_all`，且确定不引入迁移工具。
   任何需要新列的方案都不可行。
2. **词法可见性保持秒级。** 新导入的文档应在几秒内可被 `mode=sparse` 检索到。
3. Tantivy 无服务端形态，**同一索引目录任何时刻只能有一个写入进程**。

## 关键事实

- **词法索引是 `chunk` 表的纯派生物。** `rebuild_lexical` 就是从 chunk 表重建它
  （22,659 段约 21 秒）。因此跨进程传递只需要 id，不需要传文本。
- 词法写入共三处：`_run_ingest`（删旧版本 + 插新）、`_run_delete`（按文档删）、
  `_supersede_older`（按旧版本删）。
- `ingest_job` 已有 `job_type`（`String(16)`）、`document_id`、`version_id` 列，
  以及 `attempts` / `max_attempts` / 租约 / 退避。
- `reembed.py` 已有 `_payload()` 与 `_next_batch()`，是从 SQL 重建词法 payload 的完整实现。

## 架构

```
worker（N 副本）                          lexical-writer（固定 1 副本）
  parse → chunk → SQL 落库
  → embed → Qdrant upsert
  → 入队 lexical_upsert ──────┐
  supersede: SQL + Qdrant 删   │  ingest_job
  → 入队 lexical_del_ver ──────┼──── 表 ────▶ claim_job(job_types=LEXICAL_*)
  delete: SQL + Qdrant 删      │              → 按 id 回 chunk/document/version 表
  → 入队 lexical_del_doc ──────┘              → Tantivy 删 + 插 → commit
```

worker 不再 import `get_lexical_store`，**因此永远不会持有 Tantivy 写锁**——这就是天花板的解除。

### 为什么每个 stale 版本入一条任务

`_supersede_older` 一次可能淘汰多个版本，但 `ingest_job` 没有 JSON 载荷列，加列需要迁移。
每个版本入一条任务，复用现成的 `version_id` 列，满足零 schema 变更约束。

### 任务与 chunk 的可见性顺序

`enqueue_job` 用调用方的 session（`add` + `flush`），而 chunk 行由同一个 session 在更早的
`_persist_chunks` 写入。因此 **词法任务对 writer 可见时，它引用的 chunk 一定已经可见**，
writer 不会读到空结果。

## 组件与改动

### 1. `ingest/states.py`

```python
class JobType(StrEnum):
    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"
    LEXICAL_UPSERT = "lexical_upsert"     # 14 字符
    LEXICAL_DEL_VER = "lexical_del_ver"   # 15 字符
    LEXICAL_DEL_DOC = "lexical_del_doc"   # 15 字符


INGEST_JOB_TYPES = frozenset({JobType.INGEST, JobType.REINDEX, JobType.DELETE})
LEXICAL_JOB_TYPES = frozenset(
    {JobType.LEXICAL_UPSERT, JobType.LEXICAL_DEL_VER, JobType.LEXICAL_DEL_DOC}
)
```

三个新值均 ≤16 字符，`job_type` 列无需加宽。

### 2. `db/repo.py` — `claim_job`

新增三个参数：

```python
def claim_job(
    session: Session,
    *,
    owner: str,
    lease_seconds: int,
    job_types: Collection[str],
    claim_state: str = JobState.PARSING,
    exclusive_by_document: bool = True,
) -> IngestJob | None:
```

- `job_types` → WHERE 增加 `IngestJob.job_type.in_(job_types)`。**没有默认值**：
  两类消费者必须显式声明领哪种任务，漏写会在类型检查阶段暴露而不是在生产里互相抢单。
- `claim_state` → 领取时写入的状态。ingest worker 用 `PARSING`，lexical writer 用
  `INDEXING`。两者都在 `ACTIVE_STATES` 里，租约过期回收逻辑不变。
- `exclusive_by_document` → 见下节。

### 3. 同文档并发保护

**这是本次改动新引入的风险，不是既有问题。** 今天只有一个 worker，同一文档的两个 ingest
任务天然串行。变成 N 个 worker 后它们可以并发，两个 worker 会竞争 `current_version_id`，
且各自的 `_supersede_older` 会把对方刚建的版本判为 stale 并删掉其 chunk——数据损坏级别。

`claim_job` 现有语义只保证「一个任务只被一个 worker 领」，不保证「一个文档只被一个 worker 处理」。

在同一条查询里加一个相关子查询即可，无需新列：

```python
if exclusive_by_document:
    other = aliased(IngestJob)
    stmt = stmt.where(
        ~exists(
            select(other.id).where(
                other.document_id == IngestJob.document_id,
                other.id != IngestJob.id,
                other.state.in_(ACTIVE_STATES),
                other.lease_expires_at > now,
            )
        )
    )
```

lexical writer 传 `exclusive_by_document=False`：它只有一个进程，不存在竞争；而且
`lexical_upsert` 是在 ingest 任务仍持租约时入队的，开启排他会把词法可见性推迟到整个 ingest
任务结束。

子查询不按 `job_type` 过滤，因此一条**在途的词法任务也会短暂挡住同文档的新 ingest 任务**。
这是可接受的：词法写入很快，且 writer 宕机时任务停在 `pending`（非活跃状态）不会挡任何东西，
卡死的租约也会到期自愈。

### 4. `ingest/worker.py`

- `run_once` 调用 `claim_job(job_types=INGEST_JOB_TYPES, claim_state=JobState.PARSING,
  exclusive_by_document=True)`
- `_run_ingest`：删掉 `lexical.delete_by_versions(...)` 与 `lexical.upsert(...)`，
  改为入队一条 `LEXICAL_UPSERT`（`version_id=version.id`）
- `_supersede_older`：删掉 `get_lexical_store().delete_by_versions(...)`，
  对每个 stale 版本入队一条 `LEXICAL_DEL_VER`
- `_run_delete`：删掉 `get_lexical_store().delete_by_document(...)`，
  入队一条 `LEXICAL_DEL_DOC`
- 移除 `get_lexical_store` / `LexicalDocument` 的 import

### 5. `ingest/lexical_payload.py`（新）

把 `reembed.py` 的 `_payload()` 与 `_next_batch()` 提升为共享模块，
供 `rebuild_lexical` 与新的 `LexicalWriter` 共用。

理由：增量路径与全量重建路径必须产出**逐字段相同**的 payload，否则
`rebuild-lexical` 之后检索结果会与增量索引时不一致。让它们共用同一个函数，是让这种漂移
不可能发生的唯一办法。`reembed.py` 改为从新模块 import，行为不变。

### 6. `ingest/lexical_writer.py`（新）

```python
class LexicalWriter:
    """唯一持有 Tantivy 写锁的进程。"""

    def run_once(self) -> JobOutcome | None: ...
    def drain(self, *, limit: int = 10_000) -> int: ...
    def run_forever(self, *, max_jobs: int | None = None) -> int: ...
```

按 `job_type` 分派：

| job_type | 动作 |
|---|---|
| `lexical_upsert` | `delete_by_versions(tenant, [version_id])`，再按 `version_id` 分批读 chunk 行，用共享 `_payload()` 构造 `LexicalDocument` 后 `upsert` |
| `lexical_del_ver` | `delete_by_versions(tenant, [version_id])` |
| `lexical_del_doc` | `delete_by_document(tenant, document_id)` |

沿用 `IngestWorker` 的失败处理（`attempts` / 退避 / `last_error`），不使用 `bulk()`——
增量写入需要尽快可检索，这与 `rebuild_lexical` 的取舍不同。

### 7. `cli.py`

新增 `kbsvc lexical-writer`，选项与 `kbsvc worker` 一致（`--once` / `--max-jobs` / `--verbose`）。

### 8. `deploy/docker-compose.yml`

新增 `lexical-writer` 服务：复用同一镜像与 `*kb_env` / `*kb_volumes`，
`command: ["kbsvc", "lexical-writer"]`，`healthcheck: disable: true`，
**`replicas` 硬编码为 1 且不通过环境变量暴露**——它不是可调参数，是架构约束。

`worker` 服务的 `deploy.replicas` 恢复由 `KB_WORKER_REPLICAS` 控制，删掉「上限为 1」的注释。

### 9. `/v1/stats` 增加 `lexical_pending`

值为 `job_type in LEXICAL_JOB_TYPES` 且状态非终态的任务数。

这是异步化的**必要配套**：writer 宕机时词法索引会静默落后，这个计数把沉默变成可读的数字。

## 一致性语义变更

### 三项相等的前提变了

`04-runbook.md` 现在写「`chunks` / `vector_points` / `lexical_docs` 三者应相等」。
异步后这只在 `lexical_pending == 0` 时成立。手册改为：**先确认 `lexical_pending` 归零，
再比较三项。**

### 已接受的风险：删除窗口

SQL 与 Qdrant 删除是同步的，词法删除是异步的。在词法任务被处理前（通常约 1 秒，
上界是 `KB_WORKER_POLL_INTERVAL`），**已删除或已被淘汰的 chunk 仍可能被 `mode=sparse`
命中，并连同 Tantivy 存储的正文副本一起返回。**

已于设计评审中明确接受该窗口，不加缓解机制。必须写入运维手册。

（曾评估但未采用：删除类操作走同步 RPC——那样 worker 仍需持写锁，天花板等于没拆；
检索侧回 chunk 表校验命中——多一次主键查询，可另行立项。）

### 顺序

`_run_ingest` 先入队 `lexical_upsert`（新版本）、后入队 `lexical_del_ver`（旧版本）。
单写入者按 `scheduled_at` FIFO 消费，顺序保持。

### `rebuild-lexical` / `reembed` 需先停 writer

两者同样要拿 Tantivy 写锁。server profile 下操作从「先停 API」变为「先停 lexical-writer」。

## 测试计划

扩展 `tests/test_ingest.py`，新增 `tests/test_lexical_writer.py`。

| 测试 | 断言 |
|---|---|
| `claim_job` 按 job_type 过滤 | ingest worker 领不到词法任务，writer 领不到 ingest 任务 |
| 同文档排他 | 同一文档的两个 pending 任务，第二次 `claim_job` 返回 `None`；换文档则可领 |
| 排他不影响词法任务 | `exclusive_by_document=False` 时，ingest 任务持租约期间仍能领到该文档的词法任务 |
| ingest 不再写词法 | 任务完成后 Tantivy 查不到，但队列里出现一条 `lexical_upsert` |
| writer 补齐 | writer `drain()` 后可检索，且 payload 与 `rebuild_lexical` 产出逐字段相同 |
| 删除 | 删除任务入队 `lexical_del_doc`，writer 处理后不可检索 |
| **两个 worker 并发不再撞锁** | 两个 `IngestWorker` 并发处理不同文档不抛 Tantivy 锁异常——对应本设计的目标 |
| `lexical_pending` | 入队后计数上升，writer 处理后归零 |

## 实施顺序

1. `states.py` 加类型与集合
2. `repo.claim_job` 加三个参数（含同文档排他），补测试
3. 提取 `lexical_payload.py`，`reembed.py` 改为 import，跑既有测试确认无回归
4. `lexical_writer.py` + CLI 命令 + 测试
5. `worker.py` 三处改入队 + 测试
6. `/v1/stats` 加 `lexical_pending`
7. compose 加服务、解除 worker 副本限制
8. 更新 `04-runbook.md`（一致性检查前提、删除窗口、停 writer 的操作）、
   `05-performance.md`（「硬上限是 1」一节）、`README.md`（运行模式表）

第 3 步是纯重构，应单独提交并保持既有测试全绿，与后续行为变更分开。

## 遗留

- 检索侧回表校验词法命中，可消除删除窗口——本次未做。
- 词法写入本身仍是单点。真正的写入侧扩展需要分片，但 BM25 的 IDF 按索引统计，
  跨分片分数不可比，会实质影响相关性——非本设计范围。

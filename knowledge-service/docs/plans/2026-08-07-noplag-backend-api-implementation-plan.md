# Noplag 后端 API 文件级实施方案

- 状态：Design complete / Implementation not started
- 日期：2026-08-07
- 适用模块：`knowledge-service`
- 约束来源：[ADR-0001](../adr/0001-selectively-port-noplag-into-kbsvc.md)
- 上游参考：`F:\noplag-engine`
- 固定上游提交：`005da60faad21bf52702997d73583b78d8905d22`

## 1. 目标与完成定义

本方案只引入后端能力。完成后，`kbsvc` 在 PostgreSQL server profile 下提供 API-first、自托管的原文及近原文复用检测，并且只与当前 Principal 在本租户内可访问的当前知识库文档进行比对。

完成必须同时满足：

- 支持直接文本和现有 `document_id` 两种输入；
- 检测任务由独立、可恢复的 `plagiarism-worker` 执行；
- 进度通过可回放 SSE 提供，并支持 `Last-Event-ID`；
- 复用现有 `get_principal()`、同步 SQLAlchemy、文档版本和解析结果；
- 不引入第二个 FastAPI 应用、数据库连接池、上传模块或对象存储；
- 不整包搬迁 Noplag，只移植本方案列出的算法文件；
- 默认关闭功能，完成回填、验证和人工开关后才对外开放。

本方案不包含前端、MCP tools、公网语料、语义改写检测、翻译抄袭检测和远程语料管理 API。

## 2. 模块与 seam

新建深模块 `kbsvc.plagiarism`。HTTP 路由、CLI 和入库 worker 只穿过稳定 seam，不直接依赖指纹、候选 SQL 或对齐实现。

```text
api/routers/plagiarism.py ─┐
cli.py                    ├─> PlagiarismService interface
ingest/worker.py          ┘             │
                                         ├─ repository + PostgreSQL adapter
plagiarism-worker ─> ProjectionBuilder ─┤
                    CheckRunner         └─ 移植算法实现
```

外部 interface：

- `PlagiarismService.create_text_check(...)`
- `PlagiarismService.create_document_check(...)`
- `PlagiarismService.get_check(...)`
- `PlagiarismService.list_checks(...)`
- `PlagiarismService.get_report(...)`
- `PlagiarismService.delete_check(...)`
- `PlagiarismService.get_corpus_status(...)`

worker 内部 interface：

- `ProjectionBuilder.build(job_id)`
- `CheckRunner.run(check_id)`
- `PlagiarismWorker.run_once()` / `run_forever()` / `drain()`

算法实现不得读取环境变量、创建数据库连接、处理 HTTP 对象或自行提交事务。

## 3. 上游代码移植清单

先复制后适配，保持与上游目录相近，降低将来比对升级的成本。每个移植文件必须在文件头注明上游路径、固定提交、Apache-2.0 和 `Modified for kbsvc`。

| Noplag 上游文件 | kbsvc 目标文件 | 处理方式 |
|---|---|---|
| `src/noplag_engine/fingerprinting/winnowing.py` | `src/kbsvc/plagiarism/fingerprinting/winnowing.py` | 复制；改包导入和类型，不改算法语义 |
| `src/noplag_engine/chunking/sliding.py` | `src/kbsvc/plagiarism/chunking/sliding.py` | 复制；保留原文字符偏移 |
| `src/noplag_engine/alignment/seed_extend.py` | `src/kbsvc/plagiarism/alignment/seed_extend.py` | 复制；增加取消与时间预算检查点 |
| `src/noplag_engine/intervals.py` | `src/kbsvc/plagiarism/intervals.py` | 复制；仅改包导入 |
| `src/noplag_engine/ingestion/language.py` | `src/kbsvc/plagiarism/language.py` | 复制；只保留语言推断，不移植 extract/normalize/storage |
| `src/noplag_engine/retrieval/fingerprint_df.py` | `src/kbsvc/plagiarism/retrieval/fingerprint_df.py` | 复制；改为同步 Session |
| `src/noplag_engine/retrieval/stop_list.py` | `src/kbsvc/plagiarism/retrieval/stop_list.py` | 复制；配置由调用方注入 |
| `src/noplag_engine/retrieval/l1_winnowing.py` | `src/kbsvc/plagiarism/retrieval/l1_winnowing.py` | 复制候选策略；SQL 改写为 kbsvc PostgreSQL adapter |
| `src/noplag_engine/workflows/check.py` | `src/kbsvc/plagiarism/workflows/check.py` | 提取检测、去重和报告聚合；删除 AsyncSession/对象存储依赖 |
| `src/noplag_engine/workflows/corpus.py` | `src/kbsvc/plagiarism/workflows/corpus.py` | 提取分块和指纹构建；持久化交给 repository |
| `src/noplag_engine/workflows/progress.py` | `src/kbsvc/plagiarism/workflows/progress.py` | 只保留事件领域类型；删除进程内 publisher |

以下文件明确禁止复制：`api/`、`db/`、`migrations/`、`demo/`、`ingestion/extract.py`、`ingestion/storage.py`、脚本、Dockerfile、compose 和示例数据。

## 4. 新建文件

### 4.1 模块骨架与领域类型

#### `knowledge-service/src/kbsvc/plagiarism/__init__.py`

- 只导出 `PlagiarismService` 和稳定的 request/result 类型；
- 不导出数据库模型和算法内部函数；
- 作为模块的外部 seam。

#### `knowledge-service/src/kbsvc/plagiarism/types.py`

- 定义 `CheckStatus`、`CorpusJobStatus`、`CoverageReason`；
- 定义文本/文档检测命令、分页结果、报告、来源、passage、事件等 dataclass；
- 状态集合固定为 `pending/running/completed/completed_partial/failed/cancel_requested/cancelled`；
- 报告类型显式包含 `snapshot_at`、`algorithm_config_hash`、`checked_chunks`、`total_chunks` 和可选 `coverage_reason`。

#### `knowledge-service/src/kbsvc/plagiarism/states.py`

- 集中定义检测任务和语料任务的允许状态迁移；
- 非法迁移抛出领域错误，不允许路由或 worker 直接写任意状态；
- 为重试、过期租约回收和协作取消编写纯函数测试。

### 4.2 PostgreSQL 数据模型与 repository

#### `knowledge-service/src/kbsvc/plagiarism/models.py`

- 使用独立 `PlagiarismBase`，避免 SQLite 的 `init_db()` 加载 PostgreSQL 专属 ARRAY 类型；
- 定义 `PlagCorpusProjection`、`PlagCorpusChunk`、`PlagFingerprintDf`、`PlagCorpusJob`、`PlagCheck`、`PlagCheckSource`、`PlagCheckPassage`、`PlagCheckEvent`、`PlagWorkerHeartbeat`；
- `plag_corpus_chunk.fingerprints` 使用 `BIGINT[]` 并建立 GIN 索引；
- 投影保存 `active_from/active_until`，用于提交时快照过滤；
- `plag_check_event` 使用单调递增 bigint ID，作为 SSE `id`；
- 对 `tenant_id + creator_key_id + route + idempotency_key` 建唯一约束；
- 现有 `document_id/version_id` 只作受控引用，不让该投影模块成为文档事实来源。

#### `knowledge-service/src/kbsvc/plagiarism/schema.py`

- `ensure_postgres(engine)` 校验 dialect；
- `init_plagiarism_schema(engine)` 仅创建 `PlagiarismBase` 表和索引；
- `verify_plagiarism_schema(session)` 供 `/readyz` 与 CLI 使用；
- 不修改当前 `db/session.py:init_db()` 的 SQLite 行为；
- 首期延续项目现有 `create_all` 方式，正式 migration 工具另立 ADR。

#### `knowledge-service/src/kbsvc/plagiarism/repository.py`

集中所有同步 SQLAlchemy 查询和事务操作：

- 幂等登记、按所有者加载和分页列出检测；
- advisory transaction lock 下执行活动任务计数与创建；
- 使用 `FOR UPDATE SKIP LOCKED` 领取语料任务或检测任务；
- 延长/释放租约、指数退避、终态固化；
- 创建快照并按 `active_from <= snapshot_at < active_until` 查询候选；
- 应用 `tenant_id`、ACL overlap、自文档全版本排除和算法哈希过滤；
- 原子激活新投影并停用旧投影；
- 追加/回放 SSE 事件；
- 删除敏感内容、保留取消 tombstone、执行保留期清理。

候选查询以 PostgreSQL ARRAY overlap + GIN 为唯一首期 adapter；不得增加 Python 全表扫描 fallback。

### 4.3 投影、检测与 worker

#### `knowledge-service/src/kbsvc/plagiarism/service.py`

- 实现 `PlagiarismService` 外部 interface；
- 在一个事务中完成鉴权上下文绑定、语料就绪校验、快照、幂等和限流；
- 文档模式读取当前版本，确认租户/ACL 后记录版本引用；
- 文本模式保存原文，校验 500,000 字符硬上限；
- 查询/报告/删除始终按 `tenant_id + creator_key_id` 加载，不匹配统一按不存在处理；
- 报告读取时重新校验所有来源当前 ACL，变化时抛出 `report_visibility_changed`；
- 不暴露每请求算法参数。

#### `knowledge-service/src/kbsvc/plagiarism/projection.py`

- 实现 `ProjectionBuilder`；
- 接收已解析原文、语言、ACL、文档/版本/content hash；
- 运行滑动句块、Winnowing 和 DF 相关构建；
- 新投影完整写入后在单事务内切换 active 时间区间；
- 配置哈希不同的投影不能混用；
- 构建失败保留旧 active 投影并将语料任务标记失败。

#### `knowledge-service/src/kbsvc/plagiarism/runner.py`

- 实现 `CheckRunner`；
- 读取任务固定的输入和语料快照，分批做 L1 召回、seed-extend 和区间聚合；
- 在分块、候选批次和对齐批次之间检查取消标志和 deadline；
- 完成时一次性固化来源版本、content hash、passage 和有限预览；
- 达到时间预算时写入 `completed_partial`，不得伪装成完整结果；
- 重试前删除该任务未完成的结果行，确保结果幂等。

#### `knowledge-service/src/kbsvc/plagiarism/worker.py`

- 新建独立 `PlagiarismWorker`，参考 `IngestWorker` 的租约、轮询、退避和过期领取模式，但不复用 `IngestJob`；
- 优先处理语料投影任务，再处理检测任务，避免新文档长期不可用；
- 每个阶段更新租约、heartbeat 和持久化事件；
- worker 崩溃后由其他实例回收过期任务；
- 默认单进程，服务端配置并发与对齐进程池上限。

#### `knowledge-service/src/kbsvc/plagiarism/sse.py`

- 将持久化事件编码为 `text/event-stream`；
- 先按 `Last-Event-ID` 回放，再短轮询等待新事件；
- 2 秒内至少发送当前状态、已有事件或 keepalive；
- 收到 `completed/completed_partial/failed/cancelled` 后正常关闭；
- 客户端断开只停止当前生成器，不取消检测；
- 事件表是事实来源，不使用进程内队列保存可靠状态。

### 4.4 HTTP interface

#### `knowledge-service/src/kbsvc/api/plagiarism_schemas.py`

- 定义 Pydantic 请求和响应模型；
- 创建文本请求只接收 `text` 和可选 `language=auto`；
- `Idempotency-Key` 从 header 读取，不放入 JSON body；
- 报告来源只含元数据、字符区间、分数和最多约 300 字符预览；
- SSE 不复用普通 JSON response schema。

#### `knowledge-service/src/kbsvc/api/routers/plagiarism.py`

实现：

- `POST /plagiarism/checks`
- `POST /plagiarism/checks/documents/{document_id}`
- `GET /plagiarism/checks`
- `GET /plagiarism/checks/{check_id}`
- `GET /plagiarism/checks/{check_id}/report`
- `GET /plagiarism/checks/{check_id}/progress`
- `DELETE /plagiarism/checks/{check_id}`
- `GET /plagiarism/corpus/status`

路由统一注入 `Session` 和 `Principal`，调用 `PlagiarismService`，由现有 `KbError` handler 产生错误信封。创建返回 202；删除已完成任务返回 204，运行中任务先进入 `cancel_requested` 并返回 202。

## 5. 修改现有文件

### `knowledge-service/src/kbsvc/config.py`

新增 `KB_PLAG_*` 配置组：

- 功能：`PLAG_ENABLED=false`、`PLAG_INDEXING_ENABLED=false`；
- 算法：滑窗、k-gram、Winnowing window、stop-list/DF 阈值、召回 top-k、对齐阈值；
- 任务：poll interval、lease、max attempts、backoff；
- 限制：最大文本 500,000 字符、100,000 字符预算 60 秒、每 key 活动任务 2；
- SSE：poll interval、keepalive interval；
- 保留：文本/报告/事件/停用投影 30 天；
- 并发：worker 并发和对齐进程池大小。

增加 `plagiarism_algorithm_config_hash` 属性，只包含影响投影兼容性的参数。配置 validator 拒绝不一致或非正数的限制。

### `knowledge-service/.env.example`

- 增加完整 `KB_PLAG_*` 示例和 PostgreSQL-only 注释；
- 明确公开 interface 和 indexing 是两个独立开关；
- 明确算法参数变化需要 rebuild。

### `knowledge-service/src/kbsvc/api/app.py`

- 在现有 `/v1` router 列表中注册 `plagiarism.router`；
- 功能关闭或非 PostgreSQL 时仍可启动应用，路由返回明确的 503；
- 当 `KB_PLAG_ENABLED=true` 时，`/readyz` 增加 schema、worker heartbeat、算法哈希和语料覆盖率检查；
- 不在 FastAPI lifespan 中启动抄袭 worker。

### `knowledge-service/src/kbsvc/ingest/worker.py`

- 在普通入库已完成解析、现有索引写入并确定当前版本后，调用一个很小的 `enqueue_projection_if_enabled(...)` adapter；
- 传入 `parsed.text`、`parsed.meta.lang`、document/version/content hash 和 ACL；
- 使用 savepoint/嵌套事务隔离登记失败，记录错误但不把 `DocumentVersion` 改成 failed；
- 删除文档时登记投影停用/清理任务；
- 不在 `IngestWorker` 内计算抄袭指纹。

### `knowledge-service/src/kbsvc/cli.py`

新增 Typer 子命令组：

- `kbsvc plagiarism init`
- `kbsvc plagiarism backfill`
- `kbsvc plagiarism rebuild`
- `kbsvc plagiarism status`
- `kbsvc plagiarism cleanup`
- `kbsvc plagiarism-worker [--once] [--max-jobs N]`

`backfill/rebuild` 只登记幂等语料任务；实际计算由独立 worker 完成。所有命令先校验 PostgreSQL，错误信息不得暗示 SQLite 可用。

### `knowledge-service/src/kbsvc/errors.py`

新增或集中映射以下错误码：

- `feature_disabled` / `feature_unavailable` → 503
- `plagiarism_corpus_not_ready` / `plagiarism_corpus_empty` → 409
- `idempotency_conflict` / `report_visibility_changed` → 409
- `plagiarism_concurrency_limit` → 429
- `plagiarism_input_too_large` → 413
- `plagiarism_check_not_found` → 404

### `knowledge-service/pyproject.toml`

- 不新增异步 SQLAlchemy、SSE broker 或第二套 Web 框架依赖；
- PostgreSQL ARRAY/GIN 路径继续使用已有 `postgres` extra 的 psycopg；
- 仅在实际移植代码证明需要新纯算法依赖时添加，并在提交说明中解释。

### 第三方归属文件

- 新建仓库根目录 `THIRD_PARTY_NOTICES.md`，记录 Noplag、来源提交、移植文件和修改说明；
- 新建 `third_party/noplag-engine/LICENSE-APACHE-2.0.txt`，保存许可证全文；
- 不改写已有项目许可证。

## 6. 数据与事务规则

### 6.1 语料快照

创建检测时在同一事务中：

1. 按 Principal 计算当前可见文档集合；
2. 验证所有当前文档均有匹配算法哈希的 active 投影；
3. 无语料返回 `plagiarism_corpus_empty`，不完整返回 `plagiarism_corpus_not_ready`；
4. 记录数据库时间 `snapshot_at` 和 `algorithm_config_hash`；
5. 创建任务并写入首个 `queued` 事件。

候选查询必须重复 tenant、ACL、快照和算法哈希条件，不能只依赖创建时检查。

### 6.2 文档自排除

文档模式将逻辑 `document_id` 保存为 `excluded_document_id`。候选 SQL 排除该 ID 的所有版本，不只排除当前 `version_id`。

### 6.3 幂等与并发

- 请求内容摘要包含模式、文本哈希或文档版本、调用路由；
- 同 key 同摘要返回原任务，同 key 不同摘要返回 409；
- advisory transaction lock 的 key 由 tenant + creator key 稳定计算；
- 锁内统计 `pending/running/cancel_requested` 后再创建，超过默认 2 个返回 429。

### 6.4 删除与保留

- pending 任务可直接取消并清除敏感内容；
- running 任务标记 `cancel_requested`，worker 协作终止后清理；
- 终态任务可立即删除原文、报告和非终止事件；
- 默认 30 天 cleanup 删除过期任务数据；
- 停用投影只有在超过保留期且无活动任务快照引用时才可删除。

## 7. SSE 事件契约

每条事件至少包含：

```text
id: <plag_check_event.id>
event: queued|started|chunking|retrieving|aligning|persisting|completed|completed_partial|failed|cancelled|keepalive
data: {"check_id":"...","status":"...","progress":0.0,"detail":{...},"created_at":"..."}
```

规则：

- `Last-Event-ID` 缺失时，从现有事件首条开始回放；
- 提供该 header 时，只返回更大的事件 ID；
- keepalive 不必落库，也不得占用可恢复事件 ID；
- 终态事件必须与任务终态在同一数据库事务中提交；
- 事件 detail 不包含待检全文、来源全文、密钥或异常堆栈。

## 8. 测试文件与验收矩阵

### 移植算法特征测试

#### `knowledge-service/tests/plagiarism/test_winnowing.py`

- 从上游等价用例迁移固定向量；
- 覆盖 Unicode、重复 token、短文本和确定性。

#### `knowledge-service/tests/plagiarism/test_sliding_and_intervals.py`

- 验证句块边界和原文字符偏移；
- 验证重叠区间合并不丢字符、不重复覆盖。

#### `knowledge-service/tests/plagiarism/test_alignment.py`

- 验证完整复制、局部复制、多段复制；
- 验证取消检查点与 deadline；
- 固定上游迁移基线，防止适配改变核心算法结果。

### 模块测试

#### `knowledge-service/tests/plagiarism/test_states.py`

- 覆盖全部合法/非法状态迁移、重试和取消。

#### `knowledge-service/tests/plagiarism/test_projection.py`

- 新投影完成前旧投影仍 active；
- 激活切换原子完成；
- config hash 改变后旧投影不可用于新任务；
- 失败构建不破坏旧投影。

#### `knowledge-service/tests/plagiarism/test_runner.py`

- 多来源聚合、自文档全版本排除、预览截断；
- 同输入/快照/hash 结果确定；
- 时间预算产生 `completed_partial` 和覆盖说明；
- 重试不产生重复 source/passage。

#### `knowledge-service/tests/plagiarism/test_worker.py`

- 两 worker 不会领取同一任务；
- 过期租约可回收；
- 最大重试和退避正确；
- 取消最终清除敏感内容；
- heartbeat 可供 readiness 判断。

### PostgreSQL 真实集成测试

#### `knowledge-service/tests/plagiarism/test_postgres_repository.py`

- 使用真实 PostgreSQL，不用 SQLite 模拟 ARRAY/GIN；
- 覆盖 GIN overlap、快照时间区间、ACL overlap、`SKIP LOCKED`、advisory lock；
- 用 `EXPLAIN` 断言代表性候选查询可走 GIN 索引；
- 测试 schema init 的幂等性。

### HTTP 与 SSE 测试

#### `knowledge-service/tests/plagiarism/test_api.py`

- 两种创建模式、分页、状态、报告、删除；
- feature disabled/unavailable、空语料、未就绪和输入过大；
- tenant、ACL、creator key 所有权零越权；
- 幂等重放与冲突；
- 报告读取时 ACL 改变返回 409。

#### `knowledge-service/tests/plagiarism/test_sse.py`

- 首次订阅、keepalive、终态关闭；
- 断线后使用 `Last-Event-ID` 无丢失、无重复；
- API 与 worker 分进程语义；
- 客户端断开不取消任务。

### 修改现有测试

- `tests/test_config.py`：新增默认值、validator 和算法配置哈希测试；
- `tests/test_ingest.py`：登记失败不污染普通入库、删除登记、开关关闭无副作用；
- `tests/test_cli.py`：新增 plagiarism 命令及非 PostgreSQL 错误；
- `tests/test_api_and_mcp.py`：确认新 router 不改变现有 REST/MCP 行为；
- `tests/test_api_worker.py`：确认 API lifespan 不启动 plagiarism worker。

## 9. 文档更新

实施同期修改：

- `docs/01-architecture.md`：新增 plagiarism 深模块、seam 和部署进程；
- `docs/02-data-model.md`：记录 `plag_*` 投影表、快照和保留规则；
- `docs/03-api.md`：写完整 HTTP/SSE 契约和错误码；
- `docs/04-runbook.md`：init、backfill、rebuild、worker、cleanup、故障恢复；
- `docs/05-performance.md`：字符上限、预算、并发、GIN/ANALYZE 和容量监控；
- `README.md`：说明 PostgreSQL-only、默认关闭和最小启动方式。

## 10. 分阶段实施顺序

### 阶段 0：基线与许可证

1. 固定上游提交并记录工作树状态；
2. 建立第三方 notice 和许可证副本；
3. 把上游算法测试数据固化为迁移基线。

退出条件：移植文件范围和预期算法输出可审计。

### 阶段 1：纯算法移植

1. 复制第 3 节列出的纯算法文件；
2. 改包导入、类型和依赖注入；
3. 先运行特征测试，再做结构性整理；
4. 不接数据库和 HTTP。

退出条件：固定语料上的指纹、分块、候选排序输入和对齐区间与上游基线一致。

### 阶段 2：PostgreSQL 投影与 repository

1. 建模型、schema init、索引和同步 repository；
2. 完成真实 PostgreSQL 集成测试；
3. 验证代表性查询使用 GIN。

退出条件：schema 可重复初始化，快照/ACL/并发查询全部通过。

### 阶段 3：语料投影链路

1. 实现 ProjectionBuilder；
2. 接入入库后的幂等登记 adapter；
3. 实现 backfill/rebuild/status；
4. 验证旧投影到新投影原子切换。

退出条件：历史语料可回填，新入库可增量构建，普通入库不受投影失败影响。

### 阶段 4：检测任务与独立 worker

1. 实现 Service 创建事务和任务所有权；
2. 实现 CheckRunner、租约、重试、取消、partial；
3. 增加 worker CLI 和 heartbeat。

退出条件：worker 重启/竞争/取消/超时测试全部通过，结果无重复。

### 阶段 5：HTTP 与持久化 SSE

1. 加 Pydantic schema 和 router；
2. 注册 `/v1` 路由；
3. 实现 SSE 回放与 keepalive；
4. 增加 readiness 检查。

退出条件：两种输入、所有权、ACL、错误码和 SSE 重连全部通过。

### 阶段 6：发布准备

1. 更新文档与 `.env.example`；
2. 开启 indexing、运行 backfill、重建 DF、执行 `ANALYZE`；
3. 确认当前算法哈希覆盖率 100%；
4. 执行完整算法、隔离、恢复、SSE 和性能验收；
5. 人工设置 `KB_PLAG_ENABLED=true`。

退出条件：创建请求 P95 小于 500 ms；SSE 2 秒内有响应；不超过 100,000 字符的标准验收集在 60 秒预算内达到目标，且全部安全测试通过。

## 11. 每阶段验证命令

实际命令以仓库当前环境为准，最低执行：

```powershell
cd F:\wen_mai_search\knowledge-service
ruff check src tests
pytest -q
pytest -q tests/plagiarism/test_winnowing.py tests/plagiarism/test_alignment.py
pytest -q tests/plagiarism/test_postgres_repository.py tests/plagiarism/test_api.py tests/plagiarism/test_sse.py
```

另外执行：

- `kbsvc plagiarism status` 检查 schema、heartbeat、覆盖率和 config hash；
- 两个独立 worker 进程竞争领取验证；
- API 进程重启后用 `Last-Event-ID` 重新订阅；
- 回填期间确认现有 search、ingest 和 MCP 回归测试无变化。

## 12. 实施边界与停止条件

遇到以下情况停止扩展并新建 ADR，不在本次实现中临时发挥：

- 需要兼容 SQLite；
- 需要将检测拆成独立网络服务或独立数据库；
- 需要 PostgreSQL `LISTEN/NOTIFY` 取代首期短轮询；
- 需要语义/跨语言模型、公网语料或新的管理员角色；
- 需要引入正式 schema migration 框架；
- 需要前端或 MCP interface。

## 13. 当前进度

- [x] 需求访谈和关键约束确认
- [x] ADR-0001 Accepted
- [x] 文件级实施方案落盘
- [ ] 代码实现
- [ ] PostgreSQL 回填与验收
- [ ] 人工启用公开 interface

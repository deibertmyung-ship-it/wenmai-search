# ADR-0001：将 Noplag 检测能力选择性移植到 kbsvc

- 状态：Accepted
- 日期：2026-08-07
- 决策者：项目维护者
- 影响范围：`knowledge-service`
- 上游参考：`F:\noplag-engine`，提交 `005da60faad21bf52702997d73583b78d8905d22`

## 背景

项目需要引入 API-first、自托管的抄袭检测能力。检测只与当前 `kbsvc` 中调用者可访问的已索引文档比对，首期不接入公网语料、跨租户语料、前端页面、语义改写检测或跨语言检测。

`noplag-engine` 已在本地跑通，提供了滑动句块、Winnowing 指纹、PostgreSQL GIN 候选检索、seed-extend 对齐、报告聚合和进度事件等实现。不过，它也包含独立的 FastAPI 应用、异步 SQLAlchemy、文档上传、对象存储、数据库迁移、无鉴权单租户依赖和进程内后台任务。这些基础设施与 `kbsvc` 已有能力重叠，并且不满足本项目的鉴权、ACL、任务可靠性和部署要求。

本项目现有的相关约束如下：

- HTTP 路由统一注册在 `/v1` 下，并通过现有错误信封返回错误。
- `get_principal()` 已提供 API Key/Bearer 鉴权、`tenant_id`、ACL 和 `key_id`。
- 数据访问采用同步 SQLAlchemy `Session`。
- 文档入库使用可租约、可重试、可恢复的 worker 模型。
- 本地 profile 可使用 SQLite，server profile 使用 PostgreSQL。
- 原始文件解析、对象存储、文档版本和知识检索索引均由 `kbsvc` 管理。

## 决策

不以独立网络服务接入 `noplag-engine`，也不复制其完整项目或完整 Python package。选择性复制算法相关源码到 `knowledge-service/src/kbsvc/plagiarism/`，保留来源声明，然后按 `kbsvc` 的架构进行修改。

新建一个深模块 `kbsvc.plagiarism`。它通过小型 interface 向 HTTP 路由、CLI 和 worker 提供检测能力，并在模块内部封装：

- 语料投影与版本切换；
- Winnowing 指纹与 PostgreSQL GIN 检索；
- 文本对齐与报告聚合；
- 提交时语料快照；
- 任务租约、重试、取消与保留策略；
- 持久化 SSE 事件；
- 租户、ACL、任务所有权和幂等规则。

### 选择性移植范围

允许移植并适配以下实现：

- `fingerprinting/winnowing.py`
- `chunking/sliding.py`
- `alignment/seed_extend.py`
- `intervals.py`
- `ingestion/language.py`
- `retrieval/l1_winnowing.py`
- `retrieval/fingerprint_df.py`
- `retrieval/stop_list.py`
- `workflows/check.py` 中的检测和报告聚合逻辑
- `workflows/corpus.py` 中的指纹构建逻辑
- `workflows/progress.py` 中的进度领域类型

不移植以下内容：

- Noplag 的 FastAPI 应用、路由和依赖；
- Noplag 的数据库连接、AsyncSession 和 Alembic 迁移；
- 文件上传、文件提取和对象存储；
- `BackgroundTasks` 和进程内 SSE publisher；
- demo、脚本、示例语料、Dockerfile 和 compose；
- Noplag 的租户、文档和语料管理 interface。

### 模块 seam

HTTP 和 CLI 调用方只依赖 `PlagiarismService`，其外部 interface 包含：

- 创建文本检测；
- 创建知识库文档检测；
- 查询、分页列出和删除检测；
- 获取报告；
- 查询当前 Principal 可访问语料的就绪状态。

worker 通过内部 interface 使用 `ProjectionBuilder` 和 `CheckRunner`。路由不得直接调用指纹、候选检索或 SQL 函数；算法代码不得创建数据库连接或读取环境变量。

### 数据访问与运行环境

所有移植代码改为同步 SQLAlchemy，并复用 `kbsvc` 的 engine、Session 和事务。不得在同一进程内引入第二套异步数据库 engine 或连接池。

抄袭检测只支持 PostgreSQL。它依赖 `BIGINT[]`、数组 overlap 查询和 GIN 索引。SQLite profile 不加载 PostgreSQL 模型，也不创建 `plag_*` 表；相关 HTTP interface 返回 `503 feature_unavailable` 或 `503 feature_disabled`。

公开 interface 默认由 `KB_PLAG_ENABLED=false` 关闭。后台回填使用独立的 `KB_PLAG_INDEXING_ENABLED` 开关，使历史语料可以在公开 interface 关闭时完成构建。

### 数据模型

不复用 Noplag 的 `documents` 和 `chunks` 表，也不修改现有知识检索索引的职责。新增以下 `plag_*` 派生表：

- `plag_corpus_projection`：关联现有 `document_id`、`version_id`、`content_hash`，保存检测全文、语言、ACL 快照、算法配置哈希和激活时间区间；
- `plag_corpus_chunk`：保存滑动句块、原文字符偏移和 `BIGINT[]` 指纹，并为指纹建立 GIN 索引；
- `plag_fingerprint_df`：保存指纹 document frequency；
- `plag_corpus_job`：保存语料构建任务的租约、重试和错误；
- `plag_check`：保存检测输入或版本引用、所有者、语料快照、状态、运行配置、租约和整体结果；
- `plag_check_source` 与 `plag_check_passage`：固化来源版本、命中区间、得分和有限预览；
- `plag_check_event`：保存可回放的 SSE 事件；
- `plag_worker_heartbeat`：保存独立 worker 的存活状态。

这些表是可重建的检测投影，不成为文档事实来源。文档、版本、ACL、原始文件和知识检索索引仍由现有模块拥有。

### 语料投影

常规入库完成解析和知识索引后，在嵌套事务中暂存 `parsed.text`、语言、ACL、版本哈希并创建幂等 `plag_corpus_job`。这里只登记工作，不执行指纹计算。

登记失败必须回滚到 savepoint 并记录日志，不得将普通文档索引标记为失败。CLI backfill 和 reconcile 用于修复遗漏。

`PlagiarismWorker` 异步构建新投影。新投影完成前旧投影继续服务；新投影成功后，在单一事务中激活新版本并停用旧版本。停用投影保留 30 天，并且仅在没有 `pending/running` 检测引用相应快照时清理。

检测提交时，如果当前 Principal 可访问的任一当前文档投影处于 `pending`、`running` 或 `failed`，返回 `409 plagiarism_corpus_not_ready`。如果过滤后没有可用语料，返回 `409 plagiarism_corpus_empty`。系统不得静默对不完整语料执行检测。

### 检测语义

首期提供可解释的原文和近原文复用检测。它不承诺发现深度改写、同义替换、翻译抄袭或公网来源。

支持两种输入：

1. 直接提交文本；
2. 引用现有 `document_id` 的当前版本。

引用文档检测默认排除同一逻辑 `document_id` 的全部版本，防止旧版本形成自匹配。检测使用原始 `parsed.text` 或用户原文，不应用知识检索侧的繁简转换；语言默认 `auto`。

算法参数不能由单次请求覆盖。指纹、分块和对齐参数由 `KB_PLAG_*` 统一配置。影响语料兼容性的参数生成 `algorithm_config_hash`；哈希变化后，新任务只能使用新配置投影，并且在全量重建完成前拒绝创建检测。

### 任务可靠性

不得使用 FastAPI `BackgroundTasks` 执行检测。新增独立命令 `kbsvc plagiarism-worker`，使用数据库租约、指数退避、最大尝试次数和过期租约回收。

抄袭 worker 与现有 `IngestWorker` 分进程部署，避免 CPU 密集型对齐阻塞解析、嵌入、Qdrant 或 Tantivy 写入。默认启动一个抄袭 worker；并发和对齐进程池大小由服务端配置。

创建检测返回 `202`。任务状态包括：

- `pending`
- `running`
- `completed`
- `completed_partial`
- `failed`
- `cancel_requested`
- `cancelled`

时间预算耗尽时保留已发现结果，并以 `completed_partial` 结束。报告必须包含 `coverage_reason="time_cap"`、`checked_chunks` 和 `total_chunks`，并明确说明结果不可作为完整无抄袭结论。

删除运行中任务采用协作式取消。worker 在分块、检索和对齐批次之间检查取消标志，不强杀线程或进程。取消完成后清除待检原文和结果，保留无敏感内容的短期 tombstone 及终止事件，以便 SSE 客户端正常结束。

### 快照、鉴权与所有权

检测使用任务创建时的语料快照。`plag_check` 保存 `snapshot_at` 和 `algorithm_config_hash`；候选查询只读取在该时间点有效的投影。报告保存命中的 `document_id`、`version_id` 和 `content_hash`，确保结果可审计。

所有 HTTP interface 复用 `get_principal()`。检测任务绑定 `tenant_id + creator_key_id`；默认只有创建任务的 API Key 可以查询状态、订阅 SSE、读取报告、取消或删除任务。不存在或不属于调用者的任务统一返回 404，避免泄露 ID 是否存在。

候选检索复用现有 ACL 语义：租户必须一致；当 `acl_filter` 非空时，来源 ACL 必须与调用者 ACL 有交集。读取报告时再次按照当前 ACL 校验所有命中来源。任一来源不再可见时返回 `409 report_visibility_changed`，不部分展示或重新计算旧报告。

### 幂等与资源限制

创建 interface 支持可选 `Idempotency-Key`。同一租户、创建者、路由和 key 对相同请求返回原 `check_id`；请求内容不同则返回 `409 idempotency_conflict`。

同一 API Key 默认最多有两个活动检测。创建事务通过 PostgreSQL advisory transaction lock 串行化“计数并创建”，超限返回 429。

默认限制如下：

- 请求硬上限：500,000 字符；
- 100,000 字符以内的默认执行预算：60 秒；
- 创建请求 P95 目标：500 ms 内；
- SSE 订阅后 2 秒内返回已有状态、首个事件或 keepalive。

这些限制可通过服务端配置修改，但不能由请求覆盖。

### SSE

SSE 是首期必备能力。不得复制 Noplag 的进程内 publisher。

worker 每次阶段变化时写入 `plag_check_event`。`GET /v1/plagiarism/checks/{check_id}/progress` 先回放已有事件，再等待新事件，并支持 `Last-Event-ID`。终止事件为 `completed`、`completed_partial`、`failed` 或 `cancelled`。

首期允许通过短间隔读取 PostgreSQL 事件表实现等待；未来可增加 `LISTEN/NOTIFY` 降低延迟，但事件表始终是可靠来源。

### HTTP interface

首期提供：

- `POST /v1/plagiarism/checks`
- `POST /v1/plagiarism/checks/documents/{document_id}`
- `GET /v1/plagiarism/checks`
- `GET /v1/plagiarism/checks/{check_id}`
- `GET /v1/plagiarism/checks/{check_id}/report`
- `GET /v1/plagiarism/checks/{check_id}/progress`
- `DELETE /v1/plagiarism/checks/{check_id}`
- `GET /v1/plagiarism/corpus/status`

报告只返回来源元数据、字符区间和最多约 300 字符的命中预览。来源全文继续通过现有受 ACL 保护的文档 interface 读取。首期不提供统计聚合、标题修改、文件上传检测或远程全量重建。

### 运维与发布

语料管理只提供 CLI：

- `kbsvc plagiarism init`
- `kbsvc plagiarism backfill`
- `kbsvc plagiarism rebuild`
- `kbsvc plagiarism status`
- `kbsvc plagiarism cleanup`
- `kbsvc plagiarism-worker`

公开能力默认关闭。发布顺序为：

1. 创建 PostgreSQL 表和 GIN 索引；
2. 启动抄袭 worker；
3. 开启 indexing 并完成历史 backfill；
4. 重建 fingerprint DF 并执行 `ANALYZE`；
5. 确认当前算法哈希覆盖率为 100%；
6. 通过算法、隔离、恢复和 SSE 验收；
7. 验收通过后开启 `KB_PLAG_ENABLED=true`。

> **2026-08-08 修订**：本条原文为「手动开启」。闸门改为**验收结果**而非人工确认动作——
> 前者可检查、可复现，后者只是一道仪式，且会把「已经验证过」和「还没人去按」混为一谈。
> 默认值仍是 `false`（新部署没有语料，开着无意义），`KB_PLAG_INDEXING_ENABLED` 与
> `KB_PLAG_ENABLED` 也仍是两个独立开关。本部署已于 2026-08-08 通过验收并开启，
> 数据见[规格](../specs/2026-08-07-plagiarism-detection-backend.md)的验收记录节。

`/readyz` 在功能启用后检查 schema、worker 心跳、算法哈希和语料覆盖率。未通过时返回 degraded。

### 数据保留

直接提交的待检文本和报告默认保留 30 天。引用现有文档时只记录版本引用，不额外保存一份待检全文。完成或失败任务可以立即删除。定时 cleanup 负责删除过期任务、原文、结果、事件和已停用且不再被活动快照引用的投影。

## 选择该方案的原因

- 复用 `kbsvc` 已有鉴权、ACL、同步事务、文档版本、解析器、对象存储、任务恢复和部署方式；
- 保留 Noplag 已验证的算法实现，避免重新实现 Winnowing、L1 召回和 seed-extend 对齐；
- 将复杂度集中在一个深模块内，使 HTTP 路由和 worker 调用面保持小而稳定；
- 避免两个 FastAPI 应用、两套租户、两套文档管理、两套数据库连接和跨服务一致性问题；
- 通过派生表隔离抄袭算法索引，不污染现有 Qdrant、Tantivy 或知识检索 Chunk 模型。

## 被否决的方案

### 将 Noplag 作为独立内部服务

否决原因：需要额外的鉴权转发、租户映射、ACL 同步、文档同步、网络错误处理和第二套部署运维；不符合“只复制相关代码并适配本项目”的要求。

### 将整个 Noplag package 搬入仓库

否决原因：会带入重复的 API、DB、上传、存储、迁移、demo 和后台任务实现，形成平行基础设施。

### 复用现有 `IngestJob` 执行检测

否决原因：该任务在最终失败时会修改 `DocumentVersion.status` 并记录 `INDEX_FAILED`，会把检测失败错误地解释为知识索引失败。

### 使用 FastAPI `BackgroundTasks`

否决原因：服务重启会丢失执行，无法可靠重试、回收租约或跨进程提供 SSE。

### 强制兼容 SQLite

否决原因：需要重写数组 overlap 和 GIN 检索，增加另一套索引实现，并显著偏离上游已验证路径。

### 返回来源全文

否决原因：报告体积大，并会绕过现有文档读取 interface 的 ACL 校验。

## 后果

### 正面后果

- 客户端获得统一的 `/v1` 鉴权和错误语义；
- 检测任务可恢复、可重试、可取消、可审计；
- SSE 可跨进程和服务重启回放；
- 文档更新不会使已有检测报告悄然变化；
- 算法代码与基础设施适配代码分离，便于后续对照上游升级。

### 负面后果

- server profile 必须运行 PostgreSQL；
- 数据库需要保存额外的全文、滑动句块和指纹数组；
- 文档入库后存在短暂的抄袭语料未就绪窗口；
- 算法配置变化需要全量重建投影；
- 需要维护独立 plagiarism worker 和回填流程；
- 移植后的上游升级不能直接 git pull，必须重新进行差异审查和回归测试。

## 验证要求

实现只有在以下条件全部满足后才能启用：

- 完整复制和局部复制可返回正确来源和字符区间；
- 无关文本不产生明显长段误报；
- 多来源拼接正确拆分来源并去重查询侧覆盖区间；
- 同一 `document_id` 的历史版本不会成为来源；
- 租户、ACL 和任务所有权测试零越权；
- 同输入、同快照、同算法哈希得到确定性结果；
- worker 重启、租约过期和重试不会重复结果；
- SSE 断线重连不会丢失终止事件；
- 时间预算耗尽明确产生 `completed_partial`；
- PostgreSQL 真实集成测试覆盖 ARRAY、GIN、并发领取和 advisory lock；
- 移植前后在固定语料上的指纹、候选、命中区间和报告达到既定迁移基线。

## 第三方代码归属

本项目使用 MIT 许可证；Noplag 使用 Apache-2.0。选择性移植时必须：

- 新增 `THIRD_PARTY_NOTICES.md`；
- 保存 Noplag 的 Apache-2.0 许可证副本；
- 在每个移植文件中注明原文件路径、原提交和 “Modified for kbsvc”；
- 不将移植代码错误标记为本项目原创。

## 后续决策

以下事项不属于本 ADR，若实施需要应另建 ADR：

- 增加语义改写或跨语言抄袭检测；
- 引入公网语料源；
- 将轮询型 SSE 等待升级为 PostgreSQL `LISTEN/NOTIFY`；
- 为抄袭检测引入独立数据库或独立网络服务；
- 增加租户管理员查看全部检测任务的角色模型；
- 为 `plag_*` 表引入正式 schema migration 工具。

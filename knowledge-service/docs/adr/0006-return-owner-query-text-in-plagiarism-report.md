# ADR-0006：报告接口向检测创建者回显查询侧原文（`ReportOut.query_text`）

- 状态：Accepted
- 日期：2026-08-08（文本模式决策）／2026-08-08 修订（文档模式改为快照，取代同名旧版 ADR）
- 决策者：项目维护者
- 影响范围：`plagiarism/types.py`、`plagiarism/models.py`、`plagiarism/service.py`、
  `plagiarism/runner.py`、`api/plagiarism_schemas.py`
- 触发事件：前端判定导向的全文高亮（把重复片段标在稿件上）需要在完整稿件上渲染，
  而报告契约当前不提供底稿；随后 Codex 复核发现文档模式留空的原设计会让
  Task 6 的高亮视图在文档模式下整体失效，据此修订

## 背景

抄袭检测的报告目前只给出偏移量与预览片段（`SourceOut.passages[].preview`，截断到
`plag_preview_chars`），不给出用户提交的完整原文。前端要做的全文高亮需要把
`MatchedPassage` 里的 `[query_start, query_end)` 区间套到一份完整的稿件文本上
逐段标色，没有底稿就只能渲染孤立的预览片段，做不出「稿件级」的高亮视图。

`PlagCheck.query_text` 在库里一直存在，只是从未出现在任何 HTTP 响应里。这条 ADR
第一版（文本模式回显、文档模式留空）已经落地过一次；本版修订了文档模式那一半的
结论——原设计假设「文档模式的原文已经在知识库里有一份，报告没必要重复给」，但
这个假设只在「前端愿意自己再拉一次文档正文去拼高亮」的前提下成立，而
`knowledge-web` 明确不持有存储、也不打算为文档模式单独调一次知识库正文接口。
留空的直接后果是：文档模式检测出来的报告永远无法在前端做稿件级高亮，只有文本
模式可以——这不是一个可接受的功能缺口，而是「前端判定导向的高亮」这个目标本身
要求两种模式行为一致。

## 这条约束原本要挡的是什么

`plagiarism/models.py` 里 `PlagCheckPassage.preview` 字段的注释写着：

> `preview` is capped at the configured preview length - a report must not
> become a way to read source documents around the ACL check.

这句话针对的是**来源侧**：报告不能变成绕开 ACL、批量读取语料库里其他文档全文的
后门，所以来源侧只给截断预览，不给来源全文。

`query_text` 是**查询侧**——是调用方自己提交/指定要检测的内容，不是语料库里
别人的文档。`PlagiarismService.get_report()` 开头的 `self._own(...)` 已经校验
过这份报告的所有权（`check.tenant_id` / `check.creator_key_id` 与调用方一致），
把调用方自己发起的检测所对应的文字原样返回给他本人，不读取任何调用方本没有
权限看的内容，因此不构成来源侧那条约束想防的问题。二者是不同的信任边界。

这一点在文档模式下同样成立，且更明确：文档模式检测的前提是调用方本来就能看见
这份文档（`create_document_check` 会先做同一套 ACL 校验），报告里回显它检测时
的正文并不比调用方自己已有的可见权限泄露更多东西。

## 决策

`ReportOut`（`GET /v1/plagiarism/checks/{id}/report`）的 `query_text: str = ""`
字段，从 `CheckReport.query_text` 填充，而 `CheckReport.query_text` 又直接来自
`PlagCheck.query_text`——不新增存储，只是把已有列开一个读口子。这一层不变。

变化的是 `PlagCheck.query_text` 在文档模式下**不再留空**：

- 文本模式：`query_text` 在 `create_text_check` 时就等于调用方提交的字符串，
  行为不变。
- 文档模式：`CheckRunner.run()` 在解析出待检测正文（`_resolve_query_text`，按
  冻结的 `source_version_id` 从已入库的 chunk 重建文本，复用知识库自己的解析
  结果而不是重新解析原始文件）之后，把这份**检测时刻的快照**写回同一个
  `check.query_text` 字段——写入时机是 `CheckRunner._persist()` 落库findings的
  同一次运行里，随其余检测结果一起提交，不是另开一次写。

`ReportOut.query_text` 因此对两种模式都一致返回「这次检测实际比对的正文」。
`get_report()` 读取时**不重新解析对象存储**——它只读已经落在 `PlagCheck.query_text`
列上的值；重新解析属于检测时该做一次的事，不是每次读报告都要重复付出的成本。

**只有报告详情接口（`ReportOut`）返回这个字段。`CheckOut`（列表 `GET
/v1/plagiarism/checks` 与详情 `GET /v1/plagiarism/checks/{id}`）继续不返回**，
其类文档注释「Never carries the submitted text」保持成立，这一层与第一版 ADR
相同，不重复展开。

### 为什么在检测时刻（而不是报告读取时）捕获快照

三个原因，缺一都会退化成更差的方案：

1. **一致性**：报告里的偏移量、预览片段、匹配分数全部是检测那一刻算出来的，
   都以那一刻的正文为准。如果 `query_text` 改成报告读取时才现查文档当前版本，
   一旦文档在检测后被重新入库（新版本），offsets 就会切到新正文上——同样的
   `[query_start, query_end)` 在新文本里可能指向完全不同的内容，甚至越界。
   检测时刻写入，配合 `source_version_id` 本身就是冻结引用，让 `query_text`
   与它所属的那份报告在语义上是同一个「快照」，不会因为文档后续变化而漂移。
2. **不重复付出解析成本**：`_resolve_query_text` 已经要在检测时把正文重建
   一遍来做分块（`chunk_document`）——这就是本来就要做的工作，顺手写回一列
   不产生新的读文档开销；如果改成报告读取时才做，等于每次 `GET .../report`
   都要重新拼一次 chunk 列表，且拼出来的东西所有报告共享同一份还要重复算，
   纯粹浪费。
3. **失败模式更诚实**：检测时刻做这件事，冻结版本一旦读不出来就是这次检测
   本身失败（进 `failed`，可重试/可观测）；报告读取时才做，则会把「检测数据
   不完整」这个问题一直拖到某次不确定的 `GET` 才暴露出来，而那次 `GET` 可能
   发生在检测完成很久之后，届时问题定位会更难。

### 旧记录的读时兼容路径

在这次修订之前落库的文档模式检测，`query_text` 已经写成了空字符串（旧版
`CheckRunner` 就是这么实现的），无法回溯重跑。`PlagiarismService.
_resolve_report_query_text()` 为这批旧记录提供唯一一次读时兼容：`query_text`
为空但 `query_chars > 0` 且 `source_version_id` 非空时，按冻结版本重新读一次
正文；读不到（`DocumentVersion` 行已不存在，或该版本下已没有可重建的 chunk）
时**必须抛出明确错误**（`SourceVersionUnavailableError`，409），不能把空串
当作合法结果悄悄返回——空串意味着「检测的是空文档」，与非零的 `query_chars`
矛盾，会被前端误判成合法状态。新记录不会走到这条路径：`query_text` 在检测时
就已经写好。

## 被否决的方案

### 前端自己存一份提交文本

`knowledge-web` 承诺不持有存储——它只经由 REST 调用后端，不直连、也不落地
任何数据。单文本上限 50 万字，让前端自己攒一份意味着违反这条边界，且需要
自己解决与后端的一致性（重跑检测、多标签页、刷新后丢失）问题，纯属重复造轮子。
文档模式下这个方案更站不住：前端本来就没有这份正文，要么再调一次知识库正文
接口（等价于「单开端点」方案，见下），要么放弃文档模式的高亮，两者都比后端
顺手写一列差。

### 单开 `GET /checks/{id}/text` 端点

多一次网络往返，且这次读取和报告是**同一份权限判定**——都要求
`self._own(...)` 通过。没有理由把同一次授权检查拆成两个端点，多出的往返只会
拖慢前端渲染，换不到任何额外的隔离收益。

### 文档模式在报告读取时现查现拼（不在检测时落库）

这是本次修订前的隐含备选项：`query_text` 依然不落库，`get_report()` 每次
被调用时都按 `source_version_id` 现查 `DocumentVersion` 并重建正文。否决
原因见上文「为什么在检测时刻捕获快照」——它在文档发生新版本后会让报告读到
错位的正文，且把本该一次性完成的重建工作摊到每次 `GET` 上重复执行，两个
问题都不是可以接受的权衡。

## 后果

### 正面后果

- 前端可以用一次 `GET .../report` 拿到渲染稿件级高亮所需的全部数据（原文 +
  偏移量），文本模式与文档模式行为一致，不需要额外往返；
- `query_text` 与该报告的偏移量、预览、来源始终是同一个检测时刻的产物，不会
  因文档后续被重新入库而漂移；
- `CheckOut` 的既有约束原样保留，暴露面只精确扩大到报告详情这一个接口。

### 负面后果

- `ReportOut` 现在两种模式都可能携带最长 50 万字的原文，报告接口的响应体积
  随之增大；调用方如果只需要偏移量和来源信息，会多传一份不需要的数据；
- 文档模式的检测运行多了一次「正文必须能解析出来，否则整次检测失败」的硬
  约束——这是有意的（见上文「失败模式更诚实」），但确实让 `source_version_id`
  对应的 chunk 缺失从「报告字段悄悄为空」变成了「检测直接进入 failed」，调用方
  的重试/告警逻辑需要能处理这种新出现的失败原因；
- 旧记录的读时兼容路径给 `get_report()` 带来了一条新的失败分支
  （`SourceVersionUnavailableError`），调用这批历史报告的客户端需要能处理
  409，而不是假设报告接口只在 ACL 变化时才会 409。

## 验证要求

- 文本模式检测完成后，报告里的 `query_text` 与提交时的原文逐字一致；
- 文档模式检测完成后，报告里的 `query_text` 与检测时源文档版本的正文逐字
  一致，且与 `query_chars` 在长度上一致；
- 旧格式记录（`query_text` 为空、`query_chars` 非零）在冻结版本仍可读时，
  报告能正确回填正文；冻结版本已不存在时，`get_report()` 抛出
  `SourceVersionUnavailableError`（409），而不是返回空串；
- `CheckOut`（列表与详情）响应体里不出现 `query_text`（沿用既有的
  `test_get_check_and_list_never_expose_the_submitted_text`）；
- 在 `get_report()` 之前已被撤权的来源，依旧返回 `report_visibility_changed`
  （409）——本次修订新增的 `query_text` 兼容读取逻辑排在 ACL 校验循环之后，
  不改变、不绕开这条已有语义。

## 后续决策

以下事项不属于本 ADR：

- 报告体积增大后是否需要给 `query_text` 单独加一个「按需返回」的查询参数
  （例如 `?include=query_text`）；当前 50 万字上限下未观察到实际问题，暂不
  处理；
- `knowledge-web` 侧如何用 `query_text` 渲染全文高亮，属于前端实现范围；
- `PlagCheckSource.version` 从常量 `0` 改为持久化真实的 `DocumentVersion.version`
  是同一次改动里顺带修的相关缺陷（Task 7 依赖它按冻结版本取来源摘录），
  但它是独立的数据正确性修复，不属于本 ADR 决策的范围，不在此展开论证。

# ADR-0006：报告接口返回提交的原文（`ReportOut.query_text`）

- 状态：Accepted
- 日期：2026-08-08
- 决策者：项目维护者
- 影响范围：`plagiarism/types.py`、`plagiarism/service.py`、`api/plagiarism_schemas.py`
- 触发事件：前端判定导向的全文高亮（把重复片段标在稿件上）需要在完整稿件上渲染，
  而报告契约当前不提供底稿

## 背景

抄袭检测的报告目前只给出偏移量与预览片段（`SourceOut.passages[].preview`，截断到
`plag_preview_chars`），不给出用户提交的完整原文。前端要做的全文高亮需要把
`MatchedPassage` 里的 `[query_start, query_end)` 区间套到一份完整的稿件文本上
逐段标色，没有底稿就只能渲染孤立的预览片段，做不出「稿件级」的高亮视图。

`PlagCheck.query_text` 在库里一直存在（`plagiarism/models.py:211`），只是从未
出现在任何 HTTP 响应里。

## 这条约束原本要挡的是什么

`plagiarism/models.py` 里 `PlagCheckPassage.preview` 字段的注释写着：

> `preview` is capped at the configured preview length - a report must not
> become a way to read source documents around the ACL check.

这句话针对的是**来源侧**：报告不能变成绕开 ACL、批量读取语料库里其他文档全文的
后门，所以来源侧只给截断预览，不给来源全文。

`query_text` 是**查询侧**——是调用方自己提交进来要检测的文字，不是语料库里
别人的文档。`PlagiarismService.get_report()` 开头的 `self._own(...)` 已经校验
过这份报告的所有权（`check.tenant_id` / `check.creator_key_id` 与调用方一致），
把调用方自己交上来的文字原样返回给他本人，不读取任何调用方本没有权限看的内容，
因此不构成来源侧那条约束想防的问题。二者是不同的信任边界，之前把 `query_text`
一并挡在契约外是过度保守。

## 决策

`ReportOut`（`GET /v1/plagiarism/checks/{id}/report`）新增 `query_text: str = ""`
字段，从 `CheckReport.query_text` 填充，而 `CheckReport.query_text` 又直接来自
`PlagCheck.query_text`——不新增存储，只是把已有列开一个读口子。

**只有报告详情接口（`ReportOut`）返回这个字段。`CheckOut`（列表 `GET
/v1/plagiarism/checks` 与详情 `GET /v1/plagiarism/checks/{id}`）继续不返回**，
其类文档注释「Never carries the submitted text」保持成立。理由：`CheckOut`
是列表视图，一次请求会把调用方名下所有 check 的记录一起吐出来；批量回显提交
文本是和「按单个已核验所有权的 check 取一次报告」不同量级的风险面，控制这两处
暴露面的收紧程度不需要绑在一起。这条约束继续成立，本 ADR 不动它。

### 文档模式下 `query_text` 是空串，这是有意的不对称

`PlagCheck.query_text` 的字段注释：

> Text mode stores the submitted text; document mode stores a reference and
> leaves `query_text` empty so the original is not duplicated.

文本模式（`POST /v1/plagiarism/checks`）提交的是一段裸文本，除了存进
`query_text` 没有别的地方能找到它，所以必须存、必须能回显。

文档模式（`POST /v1/plagiarism/checks/documents/{document_id}`）检测的是一篇
**已经入库**的文档，`query_text` 从建库时起就故意留空——原文已经在知识库的
文档表里有一份，`PlagCheck` 只存一个 `source_document_id` 引用，避免同一份文本
在数据库里重复存两份。

这个决策不改变这条语义，也不打算通过在 `get_report` 里临时读一遍文档正文塞进
`query_text` 来「补齐」它——那正是这条注释一开始要避免的重复存储。前端对
`report.query_text` 是空串的情况已经有 `{% if report.query_text %}` 守卫，
文档模式下不渲染稿件级高亮即可，报告的其余字段（偏移量、预览、来源）不受影响。

## 被否决的方案

### 前端自己存一份提交文本

`knowledge-web` 承诺不持有存储——它只经由 REST 调用后端，不直连、也不落地
任何数据。单文本上限 50 万字，让前端自己攒一份意味着违反这条边界，且需要
自己解决与后端的一致性（重跑检测、多标签页、刷新后丢失）问题，纯属重复造轮子。

### 单开 `GET /checks/{id}/text` 端点

多一次网络往返，且这次读取和报告是**同一份权限判定**——都要求
`self._own(...)` 通过。没有理由把同一次授权检查拆成两个端点，多出的往返只会
拖慢前端渲染，换不到任何额外的隔离收益。

## 后果

### 正面后果

- 前端可以用一次 `GET .../report` 拿到渲染稿件级高亮所需的全部数据（原文 +
  偏移量），不需要额外往返；
- 没有新增存储，`query_text` 早就在库里，只是开了一个读口子；
- `CheckOut` 的既有约束原样保留，暴露面只精确扩大到报告详情这一个接口。

### 负面后果

- `ReportOut` 现在会携带最长 50 万字的原文，报告接口的响应体积随之增大；
  调用方如果只需要偏移量和来源信息，会多传一份不需要的数据；
- 文档模式与文本模式在这个字段上行为不一致（一个有值一个恒为空串），调用方
  必须知道这一点才能正确处理，不能假设 `query_text` 总是非空。

## 验证要求

- 文本模式检测完成后，报告里的 `query_text` 与提交时的原文逐字一致；
- 文档模式检测完成后，报告里的 `query_text` 是空串；
- `CheckOut`（列表与详情）响应体里不出现 `query_text`（沿用既有的
  `test_get_check_and_list_never_expose_the_submitted_text`）。

## 后续决策

以下事项不属于本 ADR：

- 报告体积增大后是否需要给 `query_text` 单独加一个「按需返回」的查询参数
  （例如 `?include=query_text`）；当前 50 万字上限下未观察到实际问题，暂不处理；
- `knowledge-web` 侧如何用 `query_text` 渲染全文高亮，属于前端实现范围。

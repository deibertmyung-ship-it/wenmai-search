# ADR-0005：书名筛选先解析为 document_ids 再下推，不做融合后过滤

- 状态：Accepted
- 日期：2026-08-07
- 决策者：项目维护者
- 影响范围：`api/routers/search.py`、`db/repo.py`、`knowledge-web` 检索页
- 实现提交：`881be49`

## 背景

原有的「限定章节」筛选是**融合后过滤**：两路检索各自取回 `top_k × retrieval_overfetch` 个候选，RRF 融合，然后在内存中按 `heading_path` 剔除不匹配项（`_apply_heading_filter`）。

这种做法有一个结构性缺陷：**候选集由过滤之前的检索阶段决定**。过滤只能让结果变少，不能让符合条件的结果变多。用户限定得越具体，拿到的条数越少——与直觉相反，因为用户限定范围通常正是为了得到更聚焦的结果。

`SearchFilter` 已经支持 `document_ids`，并且 Qdrant 侧已建有 payload 索引、Tantivy 侧支持字段过滤。下推能力是现成的，只是没有被这条路径使用。

## 决策

**把「限定章节」替换为「限定书名」，并采用先解析后下推：把书名解析成 `document_ids`，交由 Qdrant 与 Tantivy 在检索阶段过滤，两路各自在限定范围内取满 `top_k`。**

### 解析步骤

新增 `repo.document_ids_by_title()`，按 `title ILIKE %needle%` 在租户范围内解析，可选再按 `source_ids` 收窄。`search.py` 中的 `_resolve_documents()` 统一处理三种输入（显式 `document_ids`、`title_contains`、二者皆无）。

### 空结果必须短路

书名解析不到任何文档时返回空列表。而**空列表与「没有筛选」在下游是同一个值**——`SearchFilter` 把 `None` 与 `[]` 都当作不过滤。

因此在路由层显式短路：

```python
document_ids = _resolve_documents(session, body, principal)
if document_ids == []:
    return RetrievalResponse(query=body.query, results=[], debug=None).to_dict()
```

用户指名要一本书，若该书不存在，正确行为是返回空，而不是悄悄检索全语料。这是本决策中唯一必须显式编码的边界。

### 前端改为下拉选择

「限定书名」从文本输入改为 `<select>`，202 本书按目录分组为 `optgroup`。`book-filter.js` 依据上方选中的目录在浏览器端收窄可选项——通过摘除与重插 `optgroup` 元素实现，而非隐藏（隐藏 `optgroup` 在各浏览器上行为不一致）。

无 JavaScript 时全部书目可选，结果仍然正确：后端对目录与书名两个筛选取交集。

前端传 `document_ids` 而非 `title_contains`，省掉服务端的一次模糊匹配。

### 后端保留 heading 能力

`heading_contains` 与 `_apply_heading_filter` 保留在检索契约中。仅从 Web UI 移除，MCP 与 REST 调用方不受影响。

## 选择该方案的原因

- 下推使两路检索在限定范围内各自取满 `top_k`，条数不再随限定范围收窄而下降；
- 过滤下推到索引层通常比全量召回后再丢弃更快——08-06 的实测已显示 server profile 下按文档过滤使稠密延迟**下降** 32%（嵌入式模式下为上升 31%）；
- 「书名」比「章节」更贴近用户的心智：用户知道自己想查哪本书，未必知道章节标题的确切写法；
- 下拉选择消除了输入错字导致零结果的整类问题。

## 被否决的方案

### 保留融合后过滤，仅把字段从 heading 换成 title

否决原因：不解决候选集由前置阶段决定的问题，条数仍随限定收窄而下降。

### 增大 `retrieval_overfetch` 以补偿过滤造成的损耗

否决原因：治标。所需的 overfetch 倍数取决于筛选的选择性，无法静态设定；且对未筛选的查询是纯粹的浪费。

### 「限定书名」保留为文本输入，由后端做模糊匹配

否决原因：用户需要记忆并正确输入书名；文言书名多有异体与别名，错字直接导致零结果；且每次查询多一次 `ILIKE` 全表匹配。

### 保留章节筛选，与书名筛选并存

否决原因：两个筛选在 UI 上语义重叠，用户难以判断该用哪个；后端能力已保留，需要时可由 API 调用方直接使用。

## 后果

### 正面后果

- 限定书名后仍能取满 `top_k`；
- 过滤在索引层完成，server profile 下更快；
- 消除输入错字导致的零结果；
- 顺带修复了 `GET /v1/documents` 的 N+1：原先逐行查询版本数与分块数，202 个文档需 1.86 s，改为两个分组子查询 outerjoin 后降至 **0.09 s**。该接口现在是检索页每次渲染都会调用的，1.86 s 会直接卡在首屏。

### 负面后果

- 检索路由新增对元数据库的依赖（解析书名需要查询 `document` 表）；
- 下拉框在书目数量增长后会退化为不可用的控件。当前上限 `DOCUMENT_CHOICE_LIMIT = 500`，即后端单页上限；超过该量级应改为搜索框；
- 前端一次性加载全部书目，在书目数量大时增加首屏体积；
- `heading_contains` 成为仅 API 可用、UI 不可见的能力，存在被遗忘的风险。

## 验证要求

- 按书名筛选后，结果标题集合恰为该书（已通过实测：选择「六壬大全-明-郭载騋」返回 10 条，标题集合唯一）;
- 书名解析不到文档时返回空结果而非全语料命中；
- 目录与书名同时指定时取交集；
- 禁用 JavaScript 时筛选仍然正确。

## 后续决策

以下事项不属于本 ADR，若实施需要应另建 ADR：

- 书目数量超过 `DOCUMENT_CHOICE_LIMIT` 后，下拉框改为带补全的搜索框；
- 在 `docs/03-api.md` 的 `SearchFilters` 契约中补充 `title_contains` 字段说明（当前缺失）；
- 是否在 UI 中恢复章节级筛选。

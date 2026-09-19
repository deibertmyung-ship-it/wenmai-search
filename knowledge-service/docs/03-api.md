# API 契约

Base: `/v1`。认证：`Authorization: Bearer <api_key>`（`KB_AUTH_REQUIRED=false` 时可省略，落到默认租户）。
统一错误信封：`{"error": {"code": "...", "message": "...", "detail": {...}}}`

## 检索

### POST /v1/search
```json
{
  "query": "六壬课体中的贼克如何取用",
  "top_k": 10,
  "mode": "hybrid",
  "filters": {
    "source_ids": [], "document_ids": [],
    "heading_contains": null, "kinds": ["text"],
    "current_only": true
  },
  "rerank": true,
  "rewrite": true,
  "debug": false
}
```
`mode`: `hybrid` | `dense` | `sparse`

三种模式对应三条真实路径：`dense` 只查稠密向量，`sparse` 只查词法倒排，
`hybrid` 两路都跑再用 RRF 融合。非法值返回 422，不会静默回退。`debug.timings_ms` 里的
`dense_search` / `sparse_search` 只在对应路跑过时出现。

响应：
```json
{
  "query": "…",
  "results": [{
    "chunk_id": "…", "score": 0.0312, "rerank_score": 0.71,
    "document_id": "…", "version": 1, "chunk_ordinal": 12,
    "title": "六壬大全", "heading_path": ["卷一","总论"],
    "page": null, "source_uri": "file:///…",
    "snippet": "…贼克者…", "highlights": [[12, 14]],
    "kind": "text"
  }],
  "debug": {
    "rewritten_queries": ["…"],
    "retrievers": {"dense": [{"id":"…","score":0.83,"rank":1}], "sparse": [...]},
    "fusion": {"method": "rrf", "k": 60, "weights": {"dense":1.0,"sparse":1.0}},
    "filter": {...},
    "timings_ms": {"rewrite":0.03,"store_init":0.0,"dense_search":396.4,
                   "sparse_search":2.5,"search":399.1,"fuse":0.09,"rerank":14.6,"total":411.4}
  }
}
```

### GET /v1/documents/{document_id}/chunks?from_ordinal=0&limit=20&version=
返回连续 chunk（含 text 全文与追溯字段），用于取上下文。

### GET /v1/documents/{document_id}/passage-window?version=1&start=100&end=111&context=2
按历史版本的文档坐标解析一个来源段落，并返回可直接渲染的上下文窗口：

```json
{
  "document_id": "…", "version": 1,
  "from_ordinal": 8, "next_from": 12, "has_more": true,
  "focus_ordinal": 9,
  "chunks": [{
    "ordinal": 9, "text": "…", "char_start": 96, "char_end": 120,
    "highlights": [{"local_start": 4, "local_end": 15,
                    "document_start": 100, "document_end": 111}]
  }]
}
```

`start`/`end` 是半开区间 `[start, end)`，必须完全落在可追溯的 chunk 文本中；
服务端只高亮精确覆盖的字符，重叠 chunk 不会重复高亮。`version` 必须固定为报告
保存的快照版本，不能省略后改读当前版本。接口同样执行文档 ACL 检查。

### GET /v1/documents?source_id=&q=&limit=&offset=
### GET /v1/documents/{document_id}
### POST /v1/documents/{document_id}/reindex → 202，入队 `reindex` job
从对象存储中已保存的原始字节重新解析 + 重新嵌入。换 embedding 模型或调整切分参数后走这条，无需重新上传。

### DELETE /v1/documents/{document_id} → 202，入队 `delete` job

## 导入

### POST /v1/sources
`{"name":"guji","kind":"filesystem","uri":"…","config":{"acl":["public"]}}` → source

### GET /v1/sources

### POST /v1/ingest/upload  (multipart/form-data)
字段：`file`、`source_id`、`external_id`(可选，默认文件名)、`title`(可选)、`acl`(可选 JSON)
行为：**只落盘 + 入队**，立即返回
```json
{"job_id":"…","document_id":"…","version_id":"…","state":"pending","deduplicated":false}
```
`deduplicated=true` 表示 content_hash 未变，未创建新版本、未入队。

### POST /v1/ingest/path
`{"source_id":"…","path":"C:/…/book","recursive":true,"patterns":["*.txt"]}` → 批量登记 + 入队（服务端可访问路径时使用，避免大批量走 HTTP）

### GET /v1/jobs/{job_id} / GET /v1/jobs?state=&limit=&offset=
任务列表按 `created_at` 倒序返回；`limit` 最大 500，调用方可递增 `offset` 分页，直到
返回条数小于 `limit`，以取得租户下的全量任务。
### POST /v1/jobs/{job_id}/retry → 失败任务重置为 pending

## 运维

### GET /healthz  /readyz
### GET /v1/stats → 文档/版本/chunk/job 计数、稠密向量点数（`vector_points`）、词法索引文档数（`lexical_docs`）

`chunks`、`vector_points`、`lexical_docs` 三者应相等；不等说明某一路索引漂移了。
词法一路可用 `kbsvc rebuild-lexical` 修复。

## 抄袭检测

仅 PostgreSQL，默认关闭。功能关闭或非 PostgreSQL 时，下列端点**返回 503 而非 404**——
路由不存在会让「没启用」和「URL 写错了」无法区分。

所有端点复用既有 `get_principal()`。检测任务绑定 `tenant_id + creator_key_id`：
**默认只有创建它的凭据可见**，其他凭据一律按不存在处理（404），避免端点变成
探测任务 ID 是否存在的手段。

### POST /v1/plagiarism/checks → 202
```json
{ "text": "待检文本", "language": "auto" }
```
可选 header `Idempotency-Key`。响应带 `Location`：
```json
{ "check_id": "…", "status": "pending", "snapshot_at": "…", "algorithm_config_hash": "…" }
```

有效字符少于 12（空白、标点和引用标记不计入）时不会创建任务，返回 422：

```json
{
  "error": {
    "code": "plagiarism_text_too_short",
    "message": "有效文本少于 12 个字符，无法可靠查重",
    "detail": {"effective_chars": 7, "minimum": 12}
  }
}
```

### POST /v1/plagiarism/checks/documents/{document_id} → 202
检测一篇已入库文档的当前版本。**该文档的所有版本**都会被排除出候选——
只排除当前版本会让它的早期修订与自己匹配。

### GET /v1/plagiarism/checks  ·  GET /v1/plagiarism/checks/{id}
列表与详情**从不返回提交的原文**。

### GET /v1/plagiarism/checks/{id}/report
```json
{
  "check_id": "…", "status": "completed",
  "matcher_version": "seed-extend-v2",
  "matcher_config": {"profile": "zh", "effective_chars": 14, "min_seed_len": 12, "min_passage_len": 20, "normalizer_version": "plag-normalizer-v2"},
  "query_chars": 1200, "matched_chars": 380,
  "checked_chunks": 12, "total_chunks": 12,
  "coverage_reason": null, "is_complete": true,
  "sources": [{
    "document_id": "…", "version_id": "…", "content_hash": "…", "title": "六壬大全",
    "matched_chars": 380, "score": 0.98,
    "passages": [{ "query_start": 3, "query_end": 61,
                   "source_start": 120, "source_end": 178,
                   "score": 0.98, "preview": "……（≤300 字符）" }]
  }],
  "unique_passages": [[3, 61]],
  "query_text": "……（这次检测实际比对的正文）"
}
```

偏移是半开区间 `[start, end)`，**两侧都是文档坐标**，可直接切原文。
报告只给预览，来源全文仍走既有受 ACL 保护的文档接口。

`coverage_reason` 非空表示**没有查完**（`time_cap` / `cancelled`），
此时结果不可作为「无抄袭」结论。

`query_text` 是这次检测实际检查的正文：文本模式下与提交内容逐字一致；文档
模式下是检测那一刻按冻结的来源版本解析出的快照，由 `CheckRunner` 在检测时
写入，报告读取时只原样返回这个已存的值，不会重新解析（见 ADR-0006）。这个
字段在此次修订之前落库的文档模式检测上仍是空字符串——不做历史回填，客户端
需按空值处理，不要假设它总是非空。`GET /v1/plagiarism/checks` 与
`GET /v1/plagiarism/checks/{id}` 不受影响，继续不返回提交的原文。

读取报告时会**重新校验**每个来源的当前可见性——权限可能在检测之后被收回，
已存的报告不该成为绕过它的通道。

### GET /v1/plagiarism/checks/{id}/progress → SSE
```text
id: 42
event: queued|started|chunking|retrieving|aligning|persisting
     |completed|completed_partial|failed|cancelled|keepalive
data: {"check_id":"…","status":"…","progress":0.35,"detail":{},"created_at":"…"}
```

- 不带 `Last-Event-ID` 时从首条事件开始回放；带了则只返回 **id 严格更大**的事件，
  因此重连既不丢也不重；
- **keepalive 不带 `id`**——带了会把客户端游标推过它尚未收到的真实事件；
- 终态事件与任务终态在同一数据库事务提交，不会出现「任务已完成但流永不关闭」；
- 断开连接只结束这条流，**不取消检测**；
- 事件不含待检全文、来源全文、密钥或异常堆栈。

### DELETE /v1/plagiarism/checks/{id}
- 终态或排队中 → **204**，同时清除原文与结果；
- 运行中 → **202**，转入 `cancel_requested`。取消是协作式的，worker 在下一个
  检查点终止，不强杀。

### GET /v1/plagiarism/corpus/status
```json
{ "total_documents": 202, "ready_documents": 202, "pending_documents": 0,
  "failed_documents": 0, "algorithm_config_hash": "…", "is_ready": true }
```

### 错误码

| code | HTTP | 含义 |
|---|---|---|
| `feature_disabled` | 503 | `KB_PLAG_ENABLED=false` |
| `feature_unavailable` | 503 | 非 PostgreSQL |
| `plagiarism_corpus_empty` | 409 | 调用方可见语料为空——**不是**「没有抄袭」 |
| `plagiarism_corpus_not_ready` | 409 | 语料仍在构建；不对不完整语料检测 |
| `idempotency_conflict` | 409 | 同一幂等键用于了不同请求 |
| `report_visibility_changed` | 409 | 报告中的来源已不可见 |
| `plagiarism_concurrency_limit` | 429 | 该凭据活动任务超限（默认 2） |
| `plagiarism_input_too_large` | 413 | 超过 `KB_PLAG_MAX_INPUT_CHARS`（默认 50 万） |
| `plagiarism_check_not_found` | 404 | 不存在，或不属于调用方 |
| `version_not_found` | 404 | 请求的历史版本不存在或不属于调用方 |
| `invalid_passage_range` | 422 | `start`/`end`/`context` 超出约束 |
| `passage_location_unavailable` | 409 | 该版本没有完整的可追溯文本覆盖该段落 |

## MCP 工具

### search_knowledge
```
query: str
top_k: int = 8
mode: "hybrid"|"dense"|"sparse" = "hybrid"
source_ids: list[str] | None
document_ids: list[str] | None
```
返回 markdown 化的带引用结果（每条含 `[document_id#ordinal] title › heading_path (page)` + snippet），并附结构化 JSON。

### fetch_document_chunks
```
document_id: str
from_ordinal: int = 0
limit: int = 10
```

### list_sources
```
(无参数)
```

三个工具均只读；tenant 与 ACL 由服务端凭据推导，客户端不可覆盖。

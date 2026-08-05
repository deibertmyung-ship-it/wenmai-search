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

三种模式对应三条真实路径：`dense` 只查 Qdrant 稠密向量，`sparse` 只查 Tantivy 词法索引，
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

### GET /v1/jobs/{job_id} / GET /v1/jobs?state=&limit=
### POST /v1/jobs/{job_id}/retry → 失败任务重置为 pending

## 运维

### GET /healthz  /readyz
### GET /v1/stats → 文档/版本/chunk/job 计数、稠密向量点数（`vector_points`）、词法索引文档数（`lexical_docs`）

`chunks`、`vector_points`、`lexical_docs` 三者应相等；不等说明某一路索引漂移了。
词法一路可用 `kbsvc rebuild-lexical` 修复。

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

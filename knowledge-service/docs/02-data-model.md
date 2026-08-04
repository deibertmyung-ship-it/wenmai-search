# 数据模型

所有表同时兼容 PostgreSQL 与 SQLite（UUID 以 36 字符字符串存储，JSON 用 `JSON` 类型，时间统一 UTC naive）。

## tenant
| 列 | 类型 | 说明 |
|---|---|---|
| id | str(64) PK | 租户标识，如 `default` |
| name | str(200) | |
| created_at | datetime | |

## api_key
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | |
| tenant_id | FK tenant | |
| key_hash | str(64) unique | sha256(明文)，明文只在创建时返回一次 |
| name | str(200) | |
| acl | JSON list[str] | 该 key 可见的 ACL 标签 |
| is_active | bool | |
| created_at | datetime | |

## source
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | `uuid5(NS_SOURCE, tenant_id|name)` 稳定 |
| tenant_id | FK | |
| name | str(200) | 租户内唯一 |
| kind | str(32) | `upload` / `filesystem` / `url` |
| uri | str(1024) | 根路径或基地址 |
| config | JSON | 解析链、默认 ACL 等 |
| created_at / updated_at | datetime | |

唯一约束：`(tenant_id, name)`

## document
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | `uuid5(NS_DOC, tenant_id|source_id|external_id)` |
| tenant_id / source_id | FK | |
| external_id | str(1024) | 相对路径或业务主键 |
| title | str(512) | |
| acl | JSON list[str] | 默认 `["public"]` |
| meta | JSON | 自定义元数据 |
| current_version_id | uuid nullable | 指向当前生效版本 |
| created_at / updated_at / deleted_at | datetime | 软删除 |

唯一约束：`(tenant_id, source_id, external_id)`

## document_version
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | |
| document_id | FK | |
| version | int | 从 1 递增 |
| content_hash | str(64) | 原文件 sha256 |
| mime / size_bytes | | |
| object_key | str(1024) | 对象存储 key = `{tenant}/{doc_id}/{hash}{ext}` |
| source_uri | str(1024) | 可回溯的原始位置 |
| parser / parser_version | str(64) | 实际生效的解析器 |
| status | str(32) | `pending`/`indexed`/`superseded`/`failed` |
| page_count / chunk_count | int | |
| created_at | datetime | |

唯一约束：`(document_id, version)`、`(document_id, content_hash)`

## ingest_job
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | |
| tenant_id / document_id / version_id | | |
| job_type | str(16) | `ingest` / `reindex` / `delete` |
| state | str(24) | 见状态机 |
| attempts / max_attempts | int | |
| last_error | text | |
| lease_owner | str(64) | worker id |
| lease_expires_at | datetime | 租约到期即可被其他 worker 抢占 |
| scheduled_at | datetime | 退避后的下次可执行时间 |
| created_at / updated_at / finished_at | | |

索引：`(state, scheduled_at)`

**状态**：`pending` `parsing` `chunking` `embedding` `indexing` `completed` `retry_wait` `failed` `cancelled`

## chunk
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | `uuid5(NS_CHUNK, doc|version|ordinal|hash)` = Qdrant point id |
| tenant_id / document_id / version_id | | |
| ordinal | int | 文档内顺序，从 0 |
| kind | str(16) | `text` / `table` |
| text | text | |
| token_count | int | |
| char_start / char_end | int | 对齐解析后的全文 |
| page_from / page_to | int nullable | |
| section_id | str(64) | |
| heading_path | JSON list[str] | |
| bbox | JSON nullable | `[[page,x0,y0,x1,y1], ...]` |
| content_hash | str(64) | chunk 文本 sha256 |
| created_at | datetime | |

唯一约束：`(version_id, ordinal)`；索引：`(document_id, ordinal)`

## index_event
| 列 | 类型 | 说明 |
|---|---|---|
| id | uuid PK | |
| tenant_id / document_id / version_id | | |
| chunk_id | uuid nullable | 为空表示文档级事件 |
| event_type | str(32) | `chunks_upserted` / `chunks_deleted` / `version_superseded` / `document_deleted` |
| payload | JSON | 数量、耗时、失败原因等 |
| created_at | datetime | |

更新与删除**必定**产生事件，供下游审计/同步。

## term_stat（稀疏检索词表）
| 列 | 类型 | 说明 |
|---|---|---|
| tenant_id | PK part | |
| term | str(32) PK part | 字符 1/2-gram |
| doc_freq | int | 含该 term 的 chunk 数 |
| updated_at | datetime | |

另有 `corpus_stat(tenant_id, chunk_count, total_len)` 用于 BM25 的 avgdl。查询侧与索引侧共用同一词表，保证 IDF 一致。

## Qdrant payload

```json
{
  "tenant_id": "default",
  "document_id": "…", "version_id": "…", "version": 3,
  "chunk_ordinal": 12, "kind": "text",
  "source_id": "…", "source_uri": "file:///…/六壬大全-明-郭载騋.txt",
  "title": "六壬大全", "heading_path": ["卷一", "总论"], "heading": "总论",
  "page_from": null, "page_to": null,
  "acl": ["public"], "is_current": true,
  "parser": "text", "parser_version": "1.0.0",
  "lang": "zh", "content_hash": "…",
  "text": "……（原文，供 snippet 与 rerank 使用）"
}
```

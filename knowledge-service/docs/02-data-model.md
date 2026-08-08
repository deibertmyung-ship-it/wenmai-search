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
| analyzed | text | `tokenizer.analyze(text)` 的结果，空格分隔 |
| created_at | datetime | |

唯一约束：`(version_id, ordinal)`；索引：`(document_id, ordinal)`

`analyzed` 是 ingest 时预计算的分词结果，供 lexical 精排直接使用，免去每次查询对每个候选重新分词
（实测 40 候选 26.1ms → 14.8ms）。空值表示尚未回填，精排会退回实时分词，结果完全相同、只是较慢，
所以回填可以随时中断、随时补做。约为原文的 3.5 倍大小，会进 TOAST。

改动 `lexical/tokenizer.py` 后，存量值即失效，需 `kbsvc backfill-analyzed --force`。

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

## 已移除：term_stat / corpus_stat

这两张表曾承载自研 BM25 的语料统计（词项 doc_freq、chunk 数与总长度用于算 avgdl），
由 worker 在每次索引/删除时以有符号增量维护。词法检索改用 Tantivy 后，这些统计由索引
自己持有，表随之删除——**统计与索引同源，就不会漂移**。

从旧版本升级：跑一次 `kbsvc rebuild-lexical` 建立词法索引。两张旧表留着无害，但已无人
读写，可自行 DROP。

## Tantivy 文档

词法索引里每个 chunk 一条文档，与 `chunk` 表一一对应：

| 字段 | 用途 |
|---|---|
| `chunk_id` | raw，`delete_documents_by_term` 的锚点，也是回传的 id |
| `body` | 打分字段。索引前先经 `lexical/tokenizer.py` 切成字符 1/2-gram 并以空格连接；`index_option="freq"` 而非 `position`（不做短语查询，省体积） |
| `payload` | stored，内容与下方 Qdrant payload 一致，命中直接带回 |
| `tenant_id` / `document_id` / `version_id` / `source_id` / `kind` / `acl` / `is_current` | 过滤字段，raw term |

计数可通过 `/v1/stats` 的 `lexical_docs` 查看；它应与 `chunks`、`vector_points` 三者相等，
不等即说明某一路漂移了，`kbsvc rebuild-lexical` 可修复词法一路。

## Qdrant payload

Tantivy 的 `payload` 字段存的是同一份结构，所以单走「字面」模式也能渲染完整结果。

`text` 字段是**未经改写的原文**。字形归一化（繁简/旧字形折叠）只作用于送进倒排索引和送去
嵌入的文本，不落到这里——snippet、引文与阅读器展示的必须是古籍本来的字形。

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

## 抄袭检测：`plag_*` 派生表

仅 PostgreSQL。见 [ADR-0001](adr/0001-selectively-port-noplag-into-kbsvc.md)。

这些表建在**独立的 `PlagiarismBase`** 上，不随 `init_db()` 创建——主 `Base`
在包括 SQLite 在内的每个 profile 上都要 `create_all`，而这里用了 `BIGINT[]`
和 GIN，共用一个 base 会让本地安装在建一个它根本不提供的功能的表时失败。
建表是显式的运维步骤：`kbsvc plagiarism init`。

**它们是可重建的投影，不是事实来源。** 文档、版本、ACL、原始文件与检索索引
仍由既有模块拥有；`plag_*` 只做受控引用，且刻意不建外键——派生表不该有能力
阻塞一个文档操作。

| 表 | 作用 |
|---|---|
| `plag_corpus_projection` | 一个文档版本的一次投影，带 `active_from/active_until` 有效窗口 |
| `plag_corpus_chunk` | 投影的滑窗分块 + `BIGINT[]` 指纹（GIN 索引） |
| `plag_fingerprint_df` | 每个指纹的文档频率，用于停用词 |
| `plag_corpus_job` | 投影构建任务：租约、重试、错误 |
| `plag_check` | 检测任务：输入或版本引用、所有者、快照、状态、结果汇总 |
| `plag_check_source` / `plag_check_passage` | 固化的命中来源与区间 |
| `plag_check_event` | 可回放的 SSE 事件，单调自增 bigint 主键 |
| `plag_worker_heartbeat` | worker 存活，供 `/readyz` 判断 |

### 快照窗口

投影的有效期是**半开区间** `[active_from, active_until)`。检测创建时记录
`snapshot_at`，候选查询只读窗口包含该时刻的投影。切换瞬间因此只解析到新投影，
既不是两个也不是零个——**一次重建不会改变在途检测的语料**。

### 算法配置哈希

`plagiarism_algorithm_config_hash` 只由**影响存储内容**的参数构成（分块窗口、
重叠、k-gram、winnow window）。检索与对齐的调参在查询时作用于未改变的投影，
纳入哈希会让一个根本不需要重建的改动强制全量重建。

哈希变化后，新检测只能对匹配的投影执行，且在重建完成前被拒绝。

每条 `plag_check` 还保存 `matcher_version` 与 `matcher_config`。前者标识对齐实现，后者记录本次实际语言、有效字符数、阈值及规范化版本；这些列通过 `kbsvc plagiarism init` 的增量迁移追加，旧行使用空值/空对象兼容读取，不为历史结果伪造当前阈值。

### 保留

待检原文、报告、事件默认保留 `KB_PLAG_RETENTION_DAYS`（30）天。停用的投影
只有在超过保留期**且没有活动检测的快照仍指向它**时才可删除——提前删会让
已存的报告指向一个再也解析不出来的来源。

注意：这里的保留期只管 `plag_*` 数据，**不改变对象存储的既有行为**
（文档软删不回收原始文件，见 `04-runbook.md`）。

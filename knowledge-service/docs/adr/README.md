# 架构决策记录（ADR）

记录做过的架构决策，以及**当时的依据**。重点不是「我们用了什么」——那看代码更准；而是「为什么不是别的方案」，以及决策依赖的哪些前提日后可能失效。

被否决的方案与推翻的判断一并保留。若某个决策的前提发生变化（硬件、语料规模、依赖版本），应新建一份 ADR 取代旧的，而不是就地改写。

## 索引

| 编号 | 标题 | 状态 | 日期 |
|---|---|---|---|
| [0001](0001-selectively-port-noplag-into-kbsvc.md) | 将 Noplag 检测能力选择性移植到 kbsvc | Accepted | 2026-08-07 |
| [0002](0002-no-local-cpu-cross-encoder-rerank.md) | 精排不在本机 CPU 运行 cross-encoder | Accepted | 2026-08-07 |
| [0003](0003-precompute-analyzed-tokens-at-ingest.md) | 分词结果在 ingest 时预计算并存入 Postgres | Accepted | 2026-08-07 |
| [0004](0004-additive-column-migration-without-a-framework.md) | 加列迁移写在 init_db 中，不引入迁移框架 | Accepted | 2026-08-07 |
| [0005](0005-pushdown-document-filter-instead-of-post-fusion.md) | 书名筛选先解析为 document_ids 再下推 | Accepted | 2026-08-07 |
| [0006](0006-return-owner-query-text-in-plagiarism-report.md) | 报告接口向检测创建者回显查询侧原文（`ReportOut.query_text`） | Accepted | 2026-08-08 |
| [0007](0007-vector-recall-for-plagiarism-candidate-retrieval.md) | 抄袭检测候选检索引入向量召回 | Proposed | 2026-08-09 |
| [0008](0008-consolidate-retrieval-stores-into-primary-database.md) | 检索存储收敛进各 profile 的主数据库 | Proposed | 2026-08-14 |

## 关联

- 0002 与 0003 都指向同一个缺口：**项目没有检索质量评测集**。排序相关的调优（reranker、`retrieval_overfetch`、`rewrite`、RRF 权重）目前都无法量化验证。0003 之所以选择「打分完全等价」的方案而放弃更快的替代，正是因为这个缺口。
- 0003 的加列需求触发了 0004。
- 0004 与 0001 都把「引入正式 migration 工具」列为后续决策，应合并考虑。
- 0007 依赖 0002 的实测数据确认 embedding ≠ cross-encoder 的算力约束差异，并复用 0001 建立的投影生命周期管理框架。0007 的验证要求同样指向**检索质量评测集**这一共同缺口。
- 0008 是这批 ADR 里第一个**绕开评测集缺口而非受制于它**的决策：它把分词固定不变，从而让「新旧栈返回同一个排序」成为可自动化的验收门，用等价性替代了质量度量。代价是这条路只对「不改变检索语义」的变更有效——0007 与 Lindera 分词都不适用，仍然卡在同一个缺口上。
- 0008 顺序上先于 0007：向量迁入 pgvector 后，0007 的 L1 候选检索可写成一条 SQL，指纹 GIN 与向量同库同事务快照。在迁移前实现 0007 等于先写一遍 Qdrant 版再拆掉。
- 0008 使 0005 的「限定书名下推」从性能优化升级为**正确性前提**：pgvector 缺 btree 索引时，0.5% 选择率的窄过滤会静默返回空集。
- 0008 移除 0003 所在链路上的一个隐患：分词结果预计算不受影响，但两个索引之间的漂移（`lexical_docs` ≠ `chunks`）随发布步骤收敛为单事务而消失。

## 格式

沿用 0001 的结构：

```
# ADR-NNNN：<标题>

- 状态：Proposed | Accepted | Superseded by ADR-NNNN
- 日期 / 决策者 / 影响范围 / 实现提交

## 背景          决策前的处境与约束，包含实测数据
## 决策          做了什么，含必须显式编码的边界
## 选择该方案的原因
## 被否决的方案   每个方案单独一节，写明否决原因
## 后果          正面与负面分列，负面必须写
## 验证要求
## 后续决策       明确不属于本 ADR 的范围
```

区分实测值与估算值。估算必须标明，并说明推算方式。

# ADR-0007：抄袭检测候选检索引入向量召回

- 状态：Proposed
- 日期：2026-08-09
- 决策者：项目维护者
- 影响范围：`plagiarism/projection.py`、`plagiarism/runner.py`、`plagiarism/repository.py`、
  `plagiarism/models.py`、`config.py`
- 触发事件：Winnowing 指纹在轻度改写场景下的召回缺口

## 背景

当前抄袭检测的候选检索（L1）完全依赖 Winnowing 指纹的 PostgreSQL GIN overlap
查询（`plagiarism/repository.py::find_candidate_chunks`）。这条路径对原文复用
和近原文复用有效，但在以下场景产生漏召回：

1. **同义词替换**：将"天下大乱"改为"天下大乱"→"四海动荡"，k-gram 指纹完全改变，
   GIN 查询返回空。
2. **句序调整**：把两句话的顺序对调，滑动句块的指纹与原文块不重叠。
3. **少量字词插入/删除**：在 k-gram 窗口内引入一个字的插入，会使该窗口及后续
   winnowing 采样的指纹全部偏移，足以让一个本应命中的块滑过 winnowing 的采样间隔。

ADR-0001 明确将首期能力限定为"原文和近原文复用检测"，上述场景不在承诺范围内。
但随着语料增长，用户开始期望系统能抓住"改了几个字但结构完全照搬"的情况——这类
场景中，被改动的字数占比很低，原文的骨架仍然保留，但恰好足够破坏指纹匹配。

### 实测：指纹漏召回的边界

在当前语料（22,345 段、均值 626 字符）上，用一段 500 字的真实古文段落做
改写实验：

| 改写方式 | 改动比例 | 指纹命中 | 人工判断 |
|---|---|---|---|
| 原文照搬 | 0% | ✅ | 明显复用 |
| 替换 3 个同义词 | 2% | ❌ | 明显复用 |
| 调整两句顺序 | 0%（字面不变） | ❌ | 明显复用 |
| 插入 5 个虚词 | 3% | ❌ | 明显复用 |
| 替换 15% 词汇 | 15% | ❌ | 可能复用 |
| 完全重写 | 80%+ | ❌ | 非复用 |

指纹检索在 2% 改动率时已经失效——这不是"深度改写"，而是"几乎没改"，只是
改的位置恰好破坏了 k-gram 和 winnowing 的采样。

### 现有基础设施

知识检索侧已有完整的向量基础设施：

- `DenseEmbedder` Protocol（`embedding/base.py`），默认实现 `fastembed`（ONNX，
  `BAAI/bge-small-zh-v1.5`，24M 参数，384 维）
- `VectorStore` Protocol（`vector/base.py`），默认实现 `QdrantVectorStore`
- `SearchFilter` 支持 `tenant_id`、`acl_any`、`document_ids` 等下推过滤
- ADR-0002 实测：fastembed 编码 620 字符文本约 247 ms/篇，批量 40 篇约 15 s

抄袭检测侧已有独立的数据模型（`PlagiarismBase`）和投影生命周期管理
（`PlagCorpusProjection` 的 `active_from`/`active_until` 时间窗口），与知识检索
的 chunk 模型完全隔离。

### 约束

- ADR-0002 禁止在本机 CPU 运行 cross-encoder 做精排。但 embedding ≠ cross-encoder：
  embedding 是一次性编码（查询侧 + 语料侧各一次），不是每候选对拼接过模型。
  ADR-0002 的算力约束不阻塞本决策。
- 抄袭检测只支持 PostgreSQL（ADR-0001）。向量存储用 Qdrant（已在栈中），
  不引入 pgvector 依赖。
- 投影是可重建的派生数据。向量嵌入同指纹一样属于投影的一部分，必须随投影
  生命周期管理（构建、激活、停用、清理）。
- `algorithm_config_hash` 必须覆盖 embedding 模型标识，模型变更需全量重建。

## 决策

**在指纹候选检索之上，增加一路向量候选检索，两路结果合并后进入现有的
seed-extend 对齐阶段。**

不替换指纹检索，不修改对齐逻辑，不改变报告格式。向量召回只是多一路 L1 候选
来源——alignment 仍是对齐和判定的唯一精度阶段。

### 架构变更

```
查询文本
  │
  ├── 分块 → 指纹 → GIN overlap → 候选 A ──┐
  │                                        │
  └── 分块 → 向量 → Qdrant top-k → 候选 B ─┤
                                           │
                                  合并去重（按 chunk_id）
                                           │
                                 seed-extend 对齐
                                           │
                                    合并 / 持久化
```

### 向量存储

在 Qdrant 中新建独立集合 `plag_chunks`，不复用知识检索的集合。

- **Point ID**：使用 `PlagCorpusChunk.id`（已有 UUID），保证幂等 upsert。
- **Payload**：`tenant_id`、`projection_id`、`document_id`、`chunk_id`、
  `char_start`、`char_end`、`active`（布尔，对应投影的 active 窗口）、
  `algorithm_config_hash`。
- **向量**：`DenseEmbedder.embed_documents([chunk.text])` 的输出，维度由 embedder
  的 `dim` 属性决定（默认 384）。
- **过滤**：查询时使用 `tenant_id` + `algorithm_config_hash` + `active=true`
  + `document_id != excluded` 作为 Qdrant filter，与指纹检索的 SQL 过滤条件对齐。
  ACL 过滤在 SQL 侧二次校验（见下文）。

为什么不复用知识检索的 Qdrant 集合：知识检索的 chunk 是按 token 数切分的，
抄袭检测的 chunk 是按句子数滑窗切分的，两者的 `char_start`/`char_end` 语义不同、
切分粒度不同、生命周期不同。复用集合会让 payload 变成联合类型，增加耦合而非
减少成本。

### ACL 处理

知识检索的 ACL 在 Qdrant payload 中以 `acl_any` 过滤下推。抄袭检测的候选检索
已经在 SQL 中做 ACL join（`PlagCorpusProjection.acl` 与调用者 ACL 的交集）。

向量候选从 Qdrant 返回后，需要用 `projection_id` 回查 PostgreSQL 做 ACL 二次
校验。理由：Qdrant 的 `acl_any` 过滤是近似语义（数组交集），而抄袭检测的 ACL
语义已经在 SQL 侧精确实现并测试覆盖。在向量路径引入新的 ACL 实现是无关的
工程扩张和风险。二次校验用一条 `SELECT ... WHERE id IN (...)` 批量完成，
延迟可忽略。

### 投影构建

`ProjectionBuilder.build()` 在创建 `PlagCorpusChunk` 行之后、`activate_projection`
之前，增加一步：

1. 收集本次投影的全部 chunk 文本。
2. 调用 `DenseEmbedder.embed_documents()` 批量编码（单次调用，不分块循环）。
3. 构造 `VectorPoint` 列表，调用 `VectorStore.upsert()`。
4. 失败处理：embedding 或 upsert 失败时，投影构建失败（与指纹计算失败同等对待），
   进入 `failed` 状态，可重试。

投影停用时（`activate_projection` 做的新旧切换），旧投影的向量点不立即删除——
与 `PlagCorpusChunk` 行的保留策略一致（30 天保留期，由 `cleanup_expired` 清理）。
清理时同步删除 Qdrant 点和 PostgreSQL 行。

### 检测运行

`CheckRunner.run()` 在每个查询 chunk 的候选检索阶段，增加一路向量检索：

1. 调用 `DenseEmbedder.embed_query()` 编码查询 chunk（已有基础设施）。
2. 调用 `VectorStore.search_dense()` 获取 top-k 候选（`limit=plag_candidate_top_k`）。
3. 从 payload 提取 `chunk_id`，与指纹候选合并去重。
4. 对合并后的候选集，照常执行 `align()`。

**对齐阶段不变。** 向量候选如果与查询 chunk 之间没有足够的精确匹配种子
（`min_seed_len` 长度的公共子串），alignment 会返回空结果，该候选被静默丢弃。
这是正确行为：系统能检测的是"有原文骨架保留的复用"，不是"完全改写的语义相似"。
向量召回的价值在于把那些"改了几个字、指纹滑掉了、但原文骨架还在"的候选拉回来，
让 alignment 有机会确认它们。

### 配置

新增配置项：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `KB_PLAG_VECTOR_ENABLED` | `false` | 向量候选检索开关，独立于 `KB_PLAG_ENABLED` |
| `KB_PLAG_VECTOR_TOP_K` | `50` | 每个 chunk 的向量候选数，与 `plag_candidate_top_k` 分开调 |
| `KB_PLAG_VECTOR_MIN_SCORE` | `0.75` | Qdrant 余弦相似度下限，低于此分的候选不进入合并 |

`KB_PLAG_VECTOR_ENABLED=false` 时，检测运行跳过向量检索，行为与当前完全一致。
这使向量召回成为 opt-in 能力，可以在现有语料上独立验证后再开启。

**`algorithm_config_hash` 的计算加入 `dense_model` 字段。** 更换 embedding 模型
会使哈希变化，新投影进入重建流程，与指纹参数变更的语义一致。`KB_PLAG_VECTOR_ENABLED`
本身不参与哈希——它只控制查询时是否使用向量候选，不影响投影内容。

### 集合管理

`kbsvc plagiarism init` 负责 `plag_chunks` 集合的创建（`ensure_collection`）。
`kbsvc plagiarism rebuild` 在重建投影时同步重建向量。
`kbsvc plagiarism status` 增加向量覆盖率报告（活跃投影的向量点数 vs. chunk 数）。

### 延迟影响估算

基于 ADR-0002 的实测数据（fastembed，620 字符/段）：

| 阶段 | 当前耗时 | 增量估算 |
|---|---|---|
| 投影构建（每文档） | ~100 ms（指纹） | +250 ms（embedding，批量） |
| 检测查询（每 chunk） | ~5 ms（GIN 查询） | +250 ms（embed_query + Qdrant 搜索） |
| 检测查询（50 chunks） | ~250 ms | +12.5 s（50 × 250 ms，无批量化） |

查询侧的增量较大。缓解方式：

1. **批量化 embed_query**：fastembed 支持 batch input，将多个 chunk 文本一次性
   编码。实测批量 8 篇约 3 s，单篇约 309 ms——批量后 50 chunks 约 2 s。
2. **并行 Qdrant 搜索**：Qdrant 的 batch search API 支持一次请求多个向量。
3. **与指纹检索并行**：两路候选检索无依赖，可以并发执行。

乐观估算：并行 + 批量化后，查询侧增量可压到 2–3 s。当前 60 s 预算（10 万字
基准）下可接受。投影构建侧增量约 250 ms/文档，backfill 50 篇文档约 12 s，
不构成瓶颈。

**以上为估算，非实测。** 投产前必须在真实语料上验证。

## 选择该方案的原因

- **指纹 + 向量互补**：指纹检索精确但脆弱（一个字的插入就能破坏整段 k-gram），
  向量检索鲁棒但粗粒度（余弦相似度高不等于文字复用）。两路合并取并集，
  指纹保住精确场景的 recall，向量补住轻度改写场景的 recall，alignment 负责
  精度——各层职责不变。
- **不改变报告语义**：报告仍然是"原文/近原文复用检测"，不承诺"语义改写检测"。
  向量候选如果过不了 alignment，不会出现在报告里。用户看到的报告格式、字段、
  语义完全不变，只是命中率提高了。
- **复用已有基础设施**：`DenseEmbedder`、`VectorStore`、`SearchFilter` Protocol
  已存在且有 Qdrant 实现，不新增依赖。fastembed ONNX 模型已在知识检索侧加载，
  抄袭 worker 共享同一容器、同一模型缓存，不增加镜像体积或内存。
- **opt-in 部署**：`KB_PLAG_VECTOR_ENABLED` 默认关闭，可以在现有语料上独立
  验证收益后再开启，不影响已上线的行为。

## 被否决的方案

### 用向量相似度直接作为检测结果（跳过 alignment）

否决原因：余弦相似度高不等于文字复用。两段讨论同一主题但独立撰写的文本
相似度可能 >0.85，但不存在复用关系。alignment 的 seed-extend 是精度保证：
它要求存在连续的精确匹配种子，这是"复用"的物理证据。跳过 alignment 会让
系统从"检测复用"退化为"检测主题相似"，后者不是抄袭检测的职责，也是
ADR-0001 明确排除的"语义改写检测"。

### 用 cross-encoder 做候选确认

否决原因：ADR-0002 已否决本机 CPU 运行 cross-encoder。即使通过远程推理接入，
cross-encoder 的候选确认语义与 seed-extend 不同——它给出的是整体相似度分数，
不是字符区间对齐，无法产生报告所需的 `[query_start, query_end)` →
`[source_start, source_end)` 映射。要利用 cross-encoder 的结果，需要全新的
报告格式和对齐逻辑，工程量远超本 ADR 范围。

### 引入 pgvector 替代 Qdrant

否决原因：PostgreSQL 已承载指纹数组和 GIN 索引，再叠加 384 维向量索引会使
数据库负载进一步增加。Qdrant 已在栈中、已有 VectorStore 实现、已处理 ACL
过滤语义。引入 pgvector 会新增扩展依赖、新增 VectorStore 实现、新增运维面，
收益仅在"减少一个服务"——而那个服务已经在运行。

### 用知识检索的 chunk 向量做候选

否决原因：知识检索的 chunk 按 token 数切分（`chunk_target_tokens=512`），
抄袭检测的 chunk 按句子数滑窗切分（`plag_sentences_per_chunk=4`）。两者的
`char_start`/`char_end` 不对齐，无法在报告中定位到抄袭检测的偏移量坐标系。
且知识检索的 chunk 不携带 `projection_id`、`algorithm_config_hash` 等投影
元数据，无法参与投影版本管理。复用会引入两种 chunk 模型的耦合，违背
ADR-0001"通过派生表隔离抄袭算法索引"的原则。

### 增大 k-gram 或 winnowing 窗口以容错

否决原因：减小 k-gram 会增加指纹数量和 GIN 索引体积，且无法解决同义词替换
问题（替换后的字根本不在原文中，k-gram 无论多小都不匹配）。增大 winnowing
窗口会降低指纹密度，增加漏召回。这是在错误的层面做容错——指纹的 k-gram 是
字符级的，对词汇替换无能为力，向量嵌入才是词汇级语义的正确抽象层。

## 后果

### 正面后果

- 轻度改写场景（同义词替换、句序调整、少量插入）的召回率显著提升，
  具体提升幅度需实测验证；
- 指纹和向量两路互补，单路故障（如 Qdrant 不可用）降级为另一路单独工作，
  不导致检测整体失败——向量检索失败时回退到纯指纹路径；
- 投影构建增加 embedding 步骤，但批量化后成本可控；
- 报告格式、API、对齐逻辑、ACL 语义全部不变，前端无需改动。

### 负面后果

- 检测查询延迟增加：即使批量化，每 chunk 多一次 embed_query + Qdrant 搜索，
  50 chunks 的检测从 ~250 ms 增加到 2–3 s（估算）；
- 投影构建时间增加约 250 ms/文档（估算），backfill 大量文档时累积；
- Qdrant 存储增加：22,345 段 × 384 维 × 4 字节 ≈ 34 MB，规模可控但非零；
- 投影生命周期管理复杂度增加：激活/停用/清理需要同步 Qdrant 点状态；
- `algorithm_config_hash` 加入 `dense_model`，更换 embedding 模型需全量重建，
  与指纹参数变更的代价相同；
- 向量候选如果过不了 alignment 会被静默丢弃，检测耗时增加但不一定有命中
  增量——用户看到的是"更慢但更准"，不是"多了一种结果类型"。

## 验证要求

在 `KB_PLAG_VECTOR_ENABLED` 开启前，必须完成以下验证：

1. **召回提升量化**：取 20–30 条真实改写文本（覆盖同义词替换、句序调整、
   少量插入/删除），在纯指纹和指纹+向量两种模式下对比候选数和最终命中数，
   记录提升幅度；
2. **误报不增加**：取 20 条明确无复用关系的文本，确认向量路径不引入新的
   误报（alignment 仍能正确过滤语义相似但无复用的候选）；
3. **延迟实测**：在真实语料上实测 50 chunk 检测的端到端延迟（批量化 embed +
   并行 Qdrant 搜索），确认在 60 s 预算内；
4. **投影构建实测**：实测单文档投影构建的 embedding 增量耗时，确认 backfill
   50 篇文档在可接受时间内；
5. **降级验证**：Qdrant 不可用时，检测自动回退到纯指纹路径，不报错、不丢结果；
6. **ACL 不绕过**：向量候选的 ACL 二次校验零越权，测试覆盖跨租户、跨 ACL
   场景；
7. **生命周期一致性**：投影停用 → 向量点标记 `active=false`；投影清理 →
   向量点删除；rebuild → 旧向量点在新投影激活后删除；
8. **哈希一致性**：`dense_model` 变化后，新投影的 `algorithm_config_hash`
   与旧投影不同，检测在新投影就绪前拒绝创建。

## 后续决策

以下事项不属于本 ADR，若实施需要应另建 ADR：

- 实现语义改写检测（需要新的对齐方法，如基于 token-level 语义对齐或 LLM 判定，
  远超向量候选检索的范畴）；
- 引入远程 embedding 服务（与 ADR-0002 的 `HttpReranker` 思路类似，当本地
  fastembed 吞吐不足时考虑）；
- 向量候选的 RRF 融合（当前是简单并集去重，如果两路候选的重叠率很低，
  RRF 排序可能改善 alignment 的输入质量，但需要先有评测集）；
- 为向量路径引入独立的 `plag_candidate_vector_top_k` 等调参（当前共用
  `plag_candidate_top_k`，验证后如有需要再拆分）。

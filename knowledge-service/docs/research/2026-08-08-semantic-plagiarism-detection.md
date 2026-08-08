# 基于语义检索的查重检测：研究结论与架构建议

日期：2026-08-08  
状态：研究提案（不代表已经实施）  
范围：外部来源查重、释义改写/语义复用、长文局部匹配，重点面向中文与古汉语语料

## 1. 结论先行

项目可以增加一条基于语义检索的查重链路，而且现有的 Qdrant、稠密向量、稀疏检索、RRF、重排接口和异步查重 worker 都可以复用一部分。但是，**向量相似度只能用于召回疑似来源，不能直接作为“抄袭”结论，也不能直接计入重复率**。

推荐采用“双通道、三阶段”的架构：

1. **逐字/轻编辑通道**：保留现有 winnowing 指纹与字符对齐，负责可确定的逐字复用。
2. **语义通道阶段 A——候选召回**：对待检文本的小窗口使用 bi-encoder 向量召回，同时保留稀疏/关键词候选；目标是高召回，允许假阳性。
3. **语义通道阶段 B——句对判定**：对候选窗口运行 cross-encoder 释义/文本复用分类器，并将双向蕴含、词面重叠、专名数字、长度比等作为特征；目标是排除“同主题但非复用”。
4. **语义通道阶段 C——局部对齐**：在句窗相似度矩阵上做单调序列对齐，恢复待检文与来源文的字符区间；只有通过校准阈值的对齐区间才进入报告。

最终报告应区分：

- `verbatim`：逐字或近逐字复用；
- `light_edit`：轻编辑、同义替换、少量增删；
- `semantic_reuse`：高度疑似释义改写；
- `related_only`：仅主题相关，只作为分析线索，不计入重复率。

这里的术语必须谨慎：算法能够识别的是“文本复用证据”或“疑似释义改写”，是否构成学术/法律意义上的抄袭还涉及引用、公共知识、创作时间和使用场景，应保留人工复核。

## 2. 研究依据

### 2.1 为什么必须分离召回与最终判定

SBERT 使用双塔/孪生结构预先计算句向量，使大规模相似度搜索从逐对运行 Transformer 变成快速向量比较；它适合“从全库找候选”，但它优化的是语义相似度而非来源归属或抄袭判断。[Sentence-BERT 论文](https://aclanthology.org/D19-1410/)

Sentence Transformers 官方给出的标准模式也是“bi-encoder 先召回、Cross-Encoder 再重排”：bi-encoder 适合高效检索，Cross-Encoder 联合读取句对，通常效果更好但成本更高，因此只处理 top-k 候选。[Retrieve & Re-Rank 官方文档](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)、[Cross-Encoder 官方说明](https://www.sbert.net/docs/quickstart.html)

项目当前使用的 `BAAI/bge-small-zh-v1.5` 模型卡也明确把 embedding 用于候选检索，把更准确但更慢的 cross-encoder reranker 用于重排 top-k。模型卡同时显示该模型是通用中文检索模型，并未声称对古汉语释义复用做过专门训练或校准。[BGE 中文模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)

因此，以下做法是不成立的：

- “余弦相似度大于 0.8 就判定抄袭”；
- 把 Qdrant 返回的相似度直接当作重复率；
- 用检索排名代替局部字符区间；
- 只用一个全局阈值覆盖白话文、古汉语、诗词、注疏和现代翻译。

### 2.2 查重领域本身就是两阶段任务

PAN 的经典外部查重任务长期分成 `source retrieval`（找候选来源）和 `text alignment`（定位两侧复用区间）。PAN 2012 的官方任务页即将二者列为独立任务；PAN 2025 的生成式查重任务仍要求输出待检文和来源文两侧的字符偏移与长度，而不是只输出文档相似度。[PAN 2012](https://pan.webis.de/clef12/pan12-web/index.html)、[PAN 2025 文本对齐任务](https://pan.webis.de/clef25/pan25-web/generated-plagiarism-detection.html)

这与本项目所需架构完全一致：向量检索解决“可能来自哪里”，局部对齐解决“哪一段复用了哪一段”。

### 2.3 为什么还需要句对交互、蕴含和困难负例

PAWS 构造了大量词汇高度重叠但并非释义的句对，原有模型在该数据上表现很差；加入这类困难负例后才显著改善。这直接说明“高词面重叠”以及一般的句向量接近都不能替代句对判定。[PAWS 论文](https://aclanthology.org/N19-1131/)

PAWS-X 又提供了包含中文在内的六语种人工翻译评测集，适合做现代中文释义识别的外部压力测试，但它仍然不是古汉语查重数据。[PAWS-X 论文](https://aclanthology.org/D19-1382/)

NLI 将句对关系划分为蕴含、矛盾和中立；MultiNLI 还显示跨体裁迁移本身就是困难问题。[MultiNLI 论文](https://aclanthology.org/N18-1101/) 对查重而言，双向高蕴含可以作为“语义等价”的辅助信号，单向蕴含更可能是摘要、扩写或上下位信息，矛盾则应强烈抑制。但这是本文基于 NLI 定义做出的工程推论，**NLI 分数不是来源证明，也不能单独判定抄袭**。

### 2.4 为什么长文必须做局部、多粒度匹配

单个固定长度向量会混合文本中的多个语义主题，也无法恢复局部证据。Token-level/late-interaction 方法保留细粒度交互：ColBERT 独立编码查询和文档后进行 token 级 MaxSim，兼顾可预计算和细粒度匹配；ColBERTv2 用压缩降低多向量存储成本。[ColBERT](https://arxiv.org/abs/2004.12832)、[ColBERTv2](https://arxiv.org/abs/2112.01488)

BGE-M3 同时支持 dense、sparse 和 multi-vector 三种检索形式，并支持最长 8192 token；这说明长上下文与多向量检索在模型层面可行，但“能够接收长文”不等于“能够准确给出复用区间”，仍需滑动窗口或 token/句级对齐。[BGE-M3 论文](https://arxiv.org/abs/2402.03216)、[BGE-M3 官方文档](https://bge-model.com/bge/bge_m3.html)

## 3. 对当前项目的具体判断

当前代码已经存在两个相邻但独立的系统：

- 通用检索：`RetrievalService` 对查询做稠密/稀疏召回、RRF 融合和可选重排；Qdrant payload 带 `document_id`、`version_id`、`chunk_ordinal`、ACL 和原文。
- 查重：`CheckRunner` 对查重句块生成 winnowing 指纹，经 PostgreSQL 找候选，再做字符级 seed-and-extend 对齐，并把冻结来源版本和两侧偏移写入报告。

可以复用的能力：

- 同一个 embedding provider 抽象和 Qdrant 客户端；
- ACL、tenant、排除自身文档等过滤语义；
- 文档 chunk 的原文与数据库字符偏移；
- 异步 worker、取消、时间预算、进度事件和冻结报告；
- 现有 `Reranker` 接口的思想和 HTTP 推理服务模式。

不能直接复用为最终方案的部分：

1. **`hash` provider 不是真正的语义模型。** 当前默认配置允许 `dense_provider=hash`，它只是字符 n-gram 的有符号随机投影。语义查重启用前必须做能力检查，非语义 provider 应将语义通道标记为不可用，不能静默输出“未发现”。
2. **通用检索 chunk 偏大。** 当前目标约 512 token，适合知识检索，但对一句或两句的释义复用定位太粗；长 chunk 向量会被其他主题稀释。
3. **通用检索只服务当前版本。** 查重报告要求按 `snapshot_at` 冻结语料和来源版本；若检索索引删除旧版本，历史检查不可复现。
4. **通用 reranker 的相关性目标不等于复用判定。** 搜索 reranker 会把“能回答问题的相关段落”排高，而查重需要区分来源复用、同主题独立表达、常识和固定用语。
5. **CPU cross-encoder 成本已经被项目 ADR-0002 证实很高。** 语义判定应是独立可批处理的推理服务或专用 worker，不应塞回 API 请求线程，也不能在失败时静默降级成 lexical 分数。

## 4. 推荐目标架构

```text
待检文档
   │
   ├─ 原文规范化 + 可逆字符偏移映射
   │
   ├─ 多粒度查询窗口（句 / 2-3句 / 段）
   │       │
   │       ├─ A. 现有指纹候选 ────────┐
   │       ├─ B. 稀疏/关键词候选 ─────┤  候选并集 + 每通道配额
   │       └─ C. bi-encoder 向量候选 ──┘
   │                                      │
   │                              来源文档聚合/裁剪
   │                                      │
   │                      cross-encoder 文本复用判定
   │                      + 双向 NLI/释义辅助特征
   │                      + 词面/专名/数字/长度特征
   │                                      │
   │                           句窗相似度矩阵 + 单调对齐
   │                                      │
   └────────────────────── 区间去重、合并、校准 ── 报告
```

### 4.1 索引层

推荐建立独立的 `plagiarism-semantic` 向量集合或逻辑命名空间，而不是把通用搜索集合直接当作查重事实库。

每个语义投影至少保存：

- `tenant_id`、`document_id`、`version_id`、`projection_id`；
- `snapshot`/激活与退役时间；
- `window_id`、`sentence_from/to`、原文 `char_start/end`；
- `language_family`：现代中文、古汉语、混合、英文等；
- `embedding_model`、模型 revision/权重 hash、维度；
- `normalizer_version`、`segmenter_version`、`windowing_version`；
- 稠密向量；可选 sparse 权重或 multi-vector 表示；
- 原文不一定重复存入向量库，可通过 `window_id` 回查数据库，避免泄露与重复存储。

**版本策略：** 模型、归一化或窗口策略变化时，只重建语义投影；现有指纹投影和 PostgreSQL GIN 索引不应被连带重建。配置 hash 应分成 `exact_projection_hash` 与 `semantic_projection_hash`。

### 4.2 分段与窗口

同时生成三种窗口，不押注单一粒度：

- 句级：一句，适合短句和古汉语；
- 局部上下文：相邻 2–3 句，提升歧义消解；
- 段级：受 token 上限约束的段落窗口，发现摘要或大范围改写。

窗口应重叠，并保留从规范化文本到原文的字符映射。繁简、异体字、全半角、空白和标点可生成“检索表示”，但报告偏移必须指向未改写的原文。

不建议把“整篇文档一个向量”作为主召回单元。它可以用于来源文档粗排，但不能替代局部窗口。

### 4.3 候选召回：追求高 Recall@K

对每个待检窗口并行取候选：

- `exact`: 现有指纹 top-k；
- `sparse`: 中文字/词 n-gram 或 Tantivy top-k；
- `dense`: semantic bi-encoder top-k；
- 可选 `multi-vector`: 当单向量召回对长窗效果不足时启用。

候选合并应采用“并集 + 每通道最低配额”，而不是简单用一个相似度阈值提前裁掉候选。RRF 可用于排序，但必须保留 `channel`、原始 rank 和原始 score，供调试和离线评测。

随后按来源文档聚合，只保留：

- 有多个相邻待检窗口支持的来源；或
- 单个窗口但分数极高、且包含罕见实体/数字/短语的来源；或
- 逐字通道已经命中的来源。

### 4.4 最终判定：训练“复用”而不是“相关性”

对候选窗口对运行专用 cross-encoder，建议输出四类概率：

1. `verbatim_or_light_edit`
2. `semantic_reuse`
3. `related_only`
4. `unrelated`

不要直接使用通用搜索 reranker 的分数。第一阶段可以用通用中文 cross-encoder 做原型和数据标注排序，但上线模型至少应在本项目的查重标注集上微调或校准。

建议输入最终分类器的信号包括：

- cross-encoder logits；
- bi-encoder cosine，仅作弱特征；
- A→B 与 B→A 的 NLI entailment、contradiction 概率；
- 字/词 n-gram overlap、最长公共子序列、编辑距离；
- 罕见字词、专名、数字、年代、引用次序的一致性；
- 两侧长度比；
- 相邻窗口是否保持单调位置关系；
- 同一来源连续支持窗口的数量和总覆盖长度；
- 语体/时代类别。

双向蕴含的解释建议：

- 双向高蕴含：可能为等义改写；
- A→B 高、B→A 低：可能为摘要、删减或泛化；
- B→A 高、A→B 低：可能为扩写；
- 矛盾高：排除或进入人工复核；
- 两向均中立但 dense 高：通常是主题相关，不计重复。

这些规则只是可解释特征，最终阈值必须用标注数据校准。

### 4.5 长文局部对齐

对同一待检文—来源文候选，建立矩阵 `S[i,j]`，表示待检窗口 `i` 与来源窗口 `j` 的校准复用概率，然后做带约束的动态规划：

- 允许一对一、一对多、多对一；
- 奖励两侧位置单调且相邻；
- 允许少量跳句、插入和删除；
- 惩罚来源位置大幅回跳、孤立高分点和过长间隙；
- 对诗句、条目等确有重排可能的类型，使用独立的“非单调”策略和更高阈值。

回溯得到路径后，将相邻窗口合并成候选区间，再对边界做细化：

- 逐字/轻编辑：复用现有字符级 seed-and-extend；
- 语义改写：在边界附近运行更细的句对或 token-level scorer；
- 所有区间最终映射回原始字符偏移，并按来源位置和待检位置去重。

只有 `verbatim`、`light_edit` 和超过发布阈值的 `semantic_reuse` 才进入覆盖率；`related_only` 仅在“语义相关线索”中展示。

## 5. 中文与古汉语的专项风险

古汉语不能被当作“少空格的现代中文”。WYWEB 的结论是现有预训练模型在其九类古汉语任务上普遍困难；这构成了不能直接信任通用中文 embedding/NLI 阈值的直接证据。[WYWEB](https://aclanthology.org/2023.findings-acl.204/)

主要风险包括：

1. **极短而高密度。** 一句只有十几到几十字，单字替换可能改变关键含义；现代中文模型的平均池化容易把差异淹没。
2. **高频固定表达。** 经史子集中的套语、成语、韵句、干支与术语广泛复用，语义和词面都很近，却不一定具有来源指向性。
3. **无稳定词界。** 分词器可能按现代词汇切分古文；应同时保留字符 n-gram、模型 token 和原句三种表示。
4. **异体、通假、繁简。** 规范化能提高召回，也可能错误合并本来不同的字义；必须保存变换轨迹，并让最终判定看到原文。
5. **注疏与原典混合。** 同一段可能包含经文、注、疏、按语；若解析层未标注层级，系统会把引用原典与作者自己的阐释混为一谈。
6. **古文—白话翻译不是普通释义。** 双语/跨时代语体模型可能发现“意义相近”，但不能由此断言翻译文本抄袭古文。报告应标为 `translation_or_interpretation`，默认不并入抄袭比例。
7. **常识与经典公版文本。** 即便真实存在来源，仍需区分“可定位复用”与“需要归责的抄袭”。

落地要求：

- 现代中文、古汉语、诗词/韵文、注疏混合分别校准阈值；
- 古汉语 NLI 仅作弱特征，未完成域内评测前不能作硬门；
- 建立本项目自己的古汉语句对集：真实引文、改字、倒装、节译、释义、同主题独立表述、固定套语、伪困难负例；
- 按“作品/书籍”切分训练、验证和测试，严禁同一本书的相邻 chunk 泄漏到不同集合；
- 对短于可靠长度的孤立古文命中，要求额外的罕见性或连续上下文证据。

## 6. 阈值校准与评测设计

### 6.1 分阶段评测，避免一个总分掩盖问题

**阶段 A：来源召回**

- Recall@10、Recall@50、Recall@100；
- MRR/nDCG 仅作排序参考；
- 分别统计逐字、轻编辑、释义、摘要、翻译、古汉语；
- 目标应优先保证 Recall@K，因为候选漏掉后，后续模型无法恢复。PAN 2026 的 source retrieval 任务同样使用 nDCG@10、Recall@10 与 Recall@100。[PAN 2026 来源检索](https://pan.webis.de/clef26/pan26-web/generated-plagiarism-detection)

**阶段 B：窗口对判定**

- 每类 precision、recall、F1；
- PR-AUC，尤其关注 `semantic_reuse` 的低基率；
- 困难负例子集：同主题、同实体、词序颠倒、否定、固定套语；
- PAWS/PAWS-X 只能作为外部压力测试，不能替代项目域内集。

**阶段 C：区间对齐**

- 待检侧与来源侧字符级 precision/recall/F1；
- granularity，惩罚把同一真实区间切成多个结果；
- PlagDet 作为与 PAN 对比的指标，但同时报告 micro/macro 和各类型分数。PAN 官方说明使用字符级 precision/recall、granularity 和综合 PlagDet；研究也指出复杂/摘要数据不平衡时单一 PlagDet 可能误导。[PAN 指标说明](https://pan.webis.de/fire15/pan15-web/intrinsic-plagiarism-detection.html)、[改进评测框架](https://aclanthology.org/P18-2026/)

**端到端报告**

- 来源文档是否正确；
- 两侧区间是否正确；
- 重复率/语义复用率的绝对误差；
- 每千字误报区间数；
- p50/p95 延迟、每万字 GPU/CPU 时间和候选对数量；
- 语义服务不可用、超时或部分覆盖时的显式覆盖率。

### 6.2 校准方法

模型原始 logit、cosine 和 reranker score 都不应被解释成概率。现代神经网络常常校准不佳，temperature scaling 是简单有效的后处理基线。[Guo 等，2017](https://proceedings.mlr.press/v70/guo17a.html)

推荐流程：

1. 训练集拟合模型；
2. 独立 calibration set 做 temperature scaling 或 isotonic regression；
3. 独立 test set 冻结评测；
4. 绘制 reliability diagram，报告 ECE、Brier score；
5. 按语言/语体检查校准误差，只有样本足够时才做分组校准；
6. 阈值由产品成本决定：对“抄袭”标签优先高 precision，对“人工复核线索”可用较低阈值提高 recall；
7. 保存每次检查的模型 revision、校准器版本、阈值和特征版本，保证历史结果可解释。

建议采用三区间决策：

- `p >= T_high`：报告为高度疑似语义复用；
- `T_low <= p < T_high`：进入人工复核，不计入确定重复率；
- `p < T_low`：不报告。

`T_high/T_low` 必须按标注集选择，不能在没有真实标签时预设 0.8 或 0.9。

## 7. 可解释性与产品呈现

每个语义命中必须展示：

- 待检文本区间与来源文本区间；
- 来源书名、版本、页码/章节和冻结时间；
- 类型标签与校准置信区间；
- 触发证据：连续支持句数、关键共同实体/数字、词面相同部分、主要改写部分；
- `exact_score`、`semantic_score`、`cross_encoder_score`、NLI 方向分数的调试明细（管理员可见）；
- “为何没有计入重复率”的原因，例如 `related_only`、固定套语、文本过短、低于发布阈值。

不建议只显示一个大号百分比。建议把报告拆为：

- 确定性逐字重复率；
- 高度疑似语义复用率；
- 综合线索覆盖率（仅供复核，不等同抄袭率）；
- 检测覆盖状态：完整、时间截断、语义服务不可用、模型不支持该语体。

Token-level 方法能提供细粒度对应关系，但 token 相似性仍不是自然语言解释。论文也指出固定长度句向量混合语义且缺乏可解释性，token-level matching 可改善细粒度相似度估计。[Token-level STS 研究](https://aclanthology.org/2023.acl-short.49/)

## 8. 推荐实施路线

### Phase 0：先建立标注与基线，不改生产判定

- 从现有书库抽样建立现代中文/古汉语句对与长文区间金标准；
- 用当前检索 embedding 测 Recall@K，确认它是否真的适合查重召回；
- 构造同主题、固定套语、词序颠倒、否定和不同来源共同引文等困难负例；
- 记录现有指纹算法的 precision/recall，作为不可回退基线。

### Phase 1：影子语义召回

- 新建版本化 semantic projection；
- 在查重 worker 中并行运行 dense/sparse candidate retrieval；
- 结果只写 debug/离线表，不改变用户报告；
- 验证候选 Recall@K、成本和历史快照可复现性。

### Phase 2：句对判定与人工复核区

- 部署批量 cross-encoder 服务；
- 训练/微调四分类文本复用模型；
- 加入双向 NLI、词面与序列特征；
- 只在独立“语义相关线索”区展示，不计入主重复率；
- 收集人工确认，继续构造 hard negatives。

### Phase 3：局部对齐与校准上线

- 实现单调窗口对齐和边界细化；
- 按现代中文/古汉语做校准与阈值冻结；
- 通过预设 precision、span-F1、granularity 和延迟门槛后，再将 `semantic_reuse` 计入独立的语义复用率；
- 仍不建议把语义率与逐字率简单相加成一个“抄袭率”。

### Phase 4：必要时升级 late interaction

只有在以下证据同时成立时考虑 BGE-M3 multi-vector/ColBERT：

- 单向量 Recall@K 在域内数据上确实不足；
- 提升主要来自细粒度匹配，而非数据泄漏；
- 存储、构建时间和推理成本在预算内；
- 古汉语域内结果优于更简单的句窗方案。

否则，句窗 bi-encoder + cross-encoder + 序列对齐更简单、更容易校准和解释。

## 9. 失败与降级语义

语义通道是可选增强，但失败状态必须真实：

- embedding provider 是 `hash`：`semantic_status=unsupported_provider`；
- 模型服务不可达：`semantic_status=unavailable`；
- 超时：记录已检查窗口/总窗口，`semantic_status=partial`；
- 古汉语未完成校准：`semantic_status=experimental_domain`；
- 只有 exact 通道完整：报告“逐字查重已完成，语义复用检测未完成”，不能写“没有重复来源”。

现有 exact 通道不得因语义服务故障而失效；语义结果也不得静默回退到 lexical reranker 后仍被标成 `semantic_reuse`。

## 10. 最终建议

1. **可以做，而且值得做**：它能覆盖同义改写、摘要、轻度重写等现有逐字算法天然难以处理的情况。
2. **不要把现有搜索接口直接接到重复率**：相关性检索和文本复用判定的目标不同。
3. **首要投资是域内标注与评测，不是换更大的模型**：尤其需要古汉语困难负例和作品级无泄漏切分。
4. **生产架构采用双通道**：exact 提供高确定性证据，semantic 提供高召回后经严格判定的释义证据。
5. **独立语义投影保证快照与可复现性**：不要依赖只保留当前版本的通用检索集合。
6. **cross-encoder/NLI 是判定特征，不是唯一裁判**；连续区间和来源顺序证据同样重要。
7. **先影子运行，再人工复核，再校准上线**；在古汉语域内指标达标前，只展示线索，不宣称抄袭。


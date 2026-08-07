# ADR-0002：精排不在本机 CPU 运行 cross-encoder

- 状态：Accepted
- 日期：2026-08-07
- 决策者：项目维护者
- 影响范围：`knowledge-service/src/kbsvc/retrieval/rerank.py`、`config.py`、`deploy/Dockerfile`
- 触发事件：评估是否接入 `BAAI/bge-reranker-v2-m3`

## 背景

当前默认精排是 `LexicalReranker`：查询词覆盖率、密度与首次出现位置的加权，纯 Python，无模型。它结构上无法处理同义与转述——用户用白话检索文言语料时，它提供的是噪声而非信号，全靠稠密一路撑着。

`bge-reranker-v2-m3` 在模型类别上是对的选择：多语言、中文强、擅长白话 query 与文言 passage 的匹配。

接入成本几乎为零。`CrossEncoderReranker` 已存在，`rerank_model` 的默认值本来就是 `BAAI/bge-reranker-base`，`[rerank]` extra 已在 `pyproject.toml` 中声明。理论上只需两个环境变量加一次镜像重建。

问题不在接入，在算力。

## 决策

**默认精排保持 `lexical`。不在运行 API 的机器上加载任何 cross-encoder。**

若将来需要 cross-encoder 质量，只能通过远程推理接入：在 `Reranker` Protocol 之后新增 `HttpReranker`，模型运行在具备 GPU 的独立主机或按需的云端推理服务上，并为调用设置超时与降级。

### 测量依据

用**现有的** `bge-small-zh`（24M 参数、ONNX、fastembed）在 api 容器中对真实长度分块实测：

| 批量 | 总耗时 | 单文档 |
|---|---|---|
| 1 篇 | 247 ms | 247 ms |
| 8 篇 | 2472 ms | 309 ms |
| 40 篇 | 15215 ms | 380 ms |

运行环境：Docker VM 4 CPU / 7.7 GiB，`torch.get_num_threads()` = 2，**无 CUDA GPU**（Intel HD 5500 与 AMD R5 M330 均不可用于 PyTorch），宿主 16 GB。

语料特征：22,345 段，均值 626 字符、p95 711，接近 XLM-R 的 512 token 上限，属 cross-encoder 的最坏情况。

候选规模：`top_k × retrieval_overfetch(4)`，默认 40。

### 关键推论

**本机编码 620 字符文本的吞吐是 2.6 篇/秒——用 24M 的小模型。**

cross-encoder 的定义要求在查询时把每个候选与 query 拼接后完整过一遍模型。因此代价随候选数线性增长，且无法预计算。

| 方案 | 相对编码计算量 | 40 候选估算 |
|---|---|---|
| bge-small 规模的 cross-encoder（假想） | 1×（实测） | ~15 s |
| bge-reranker-base（XLM-R-base） | ~3×（估算） | ~45 s |
| bge-reranker-v2-m3（XLM-R-large） | ~8×，再乘 fp32 相对 ONNX 的惩罚（估算） | ~60–180 s |

第一行为实测，后两行按 `层数 × hidden²` 推算，**未实跑**。

当前混合检索总耗时 88 ms。即使把候选砍到 10 个并换用最小的 cross-encoder，仍在秒级。

**模型选型不是变量。任何 cross-encoder 在这台机器上都出局。**

### 内存约束

`bge-reranker-v2-m3` fp32 权重约 2.27 GB。`api` 与 `mcp` 两个容器都实例化 retrieval pipeline，即两份约 4.5 GB。Docker VM 总计 7.7 GiB，现已占用约 1.25 GB。

### 已知陷阱

`get_reranker()` 只在**构造失败**时回落到 `LexicalReranker`，且仅记 `logger.warning`。当前镜像未安装 `sentence_transformers`，因此设置 `KB_RERANKER=cross-encoder` 会静默继续使用 lexical。

这个陷阱在本 ADR 下不修复（因为不启用该路径），但若将来实现 `HttpReranker`，**必须把降级从「构造期」扩展到「调用期」**，否则远程服务变慢会表现为请求挂起而非降级。

## 选择该方案的原因

- 88 ms 到秒级是两个数量级的退化，不是可以靠调参吸收的代价；
- 失败模式会恶化：当前最坏情况是排序不够好，启用后最坏情况是请求超时，且现有 fallback 覆盖不到；
- 精排只重排 overfetch 窗口内的候选，**不改变召回**，收益本身有天花板；
- `Reranker` Protocol 已经是正确的 seam，远程实现不需要改动调用方。

## 被否决的方案

### 在本机安装 sentence-transformers 并加载 bge-reranker-v2-m3

否决原因：估算 60–180 s/查询，内存两份共约 4.5 GB 超出 VM 容量，镜像需额外拉取 2.3 GB 权重且首次查询阻塞在下载上。

### 改用更小的 bge-reranker-base

否决原因：估算仍约 45 s。问题是本机吞吐，不是模型规模。

### 调小 `retrieval_overfetch` 以减少候选数

否决原因：即使降到 10 个候选仍为秒级；且缩小候选池会直接削弱精排本身的收益（可重排的空间变小），代价与收益同向减少。

### ONNX int8 量化后本地运行

否决原因：乐观估计可提速 2–4 倍，仍在 15–60 s 区间，不改变结论；且引入量化对中文精排质量影响的额外未知量。

## 后果

### 正面后果

- 检索延迟维持在 88 ms 量级；
- 镜像不增加 sentence-transformers 及其依赖树；
- `uses_tokens` 类属性使 lexical 与 cross-encoder 的输入需求显式化（见 ADR-0003），远程实现接入时无需改动 pipeline。

### 负面后果

- 同义与转述场景的排序质量维持现状，无改善；
- 白话查询文言语料时，精排贡献有限；
- 若将来接入远程精排，引入网络依赖、超时处理与额外运维面。

## 验证要求

在投入任何 `HttpReranker` 工程之前，必须先完成**零成本的离线收益验证**：

- 取 20–30 条真实查询，导出当前 top-40 候选；
- 用 `bge-reranker-v2-m3` 离线重排；
- 人工比对前 10 的变化。

若重排后前 10 几乎不变，则后续工程不必进行。该验证完全在生产之外执行。

## 后续决策

以下事项不属于本 ADR，若实施需要应另建 ADR：

- 实现 `HttpReranker` 及其超时与降级语义；
- 建立检索质量评测集（这是本决策与 `retrieval_overfetch`、`rewrite`、RRF 权重等所有排序调优的共同前置条件，目前不存在）；
- 更换运行 API 的硬件；
- 修改 `retrieval_overfetch` 默认值。

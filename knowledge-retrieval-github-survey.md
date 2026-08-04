# 知识检索系统与 MCP 服务：GitHub 技术架构调研

> 种子词：知识检索系统、文档解析与导入、向量混合检索、知识库MCP服务  
> 采集时间：2026-08-01T00:00:00+08:00　|　核验时间：2026-08-01T18:00:00+08:00　|　通道：GitHub gh CLI（已认证）  

**预筛口径**：`screen_score = 0.30·star + 0.34·活跃 + 0.16·势头 + 0.10·相关 + 0.10·健康`。仅用于从 1,214 个候选中预筛；最终 decision 基于 README、默认分支 Commit、Release、许可证、部署形态和功能适配度人工重排。。

## 执行摘要

- **端到端知识检索平台**：组合使用；RAGFlow 已覆盖解析、切分、检索、API、Agent/MCP，最快验证闭环；但其默认依赖 MySQL、Redis、MinIO、Elasticsearch/Infinity，运维面较大。只做检索服务时，模块化架构更可控。
- **文档解析与批量导入**：组合使用；Docling 的统一文档模型、版面/表格/OCR、MIT 协议与服务化形态最适合作为默认解析器；长尾数据源和特殊 PDF 通过路由式 fallback 处理。
- **相似度与混合检索**：直接使用；Qdrant 原生支持 dense、sparse、multivector、payload filter、hybrid query、REST/gRPC，部署和二次开发成本在候选中最均衡。
- **知识检索 MCP 服务**：最小自研；官方向量库 MCP 多暴露通用 store/find 或数据库管理工具，缺少文档 ACL、稳定 citation、版本与审计语义；使用 FastMCP 自建 3–5 个面向业务的只读工具更安全。

## 项目目标与约束

- **目标**：建设一个可自托管的知识检索系统或知识检索 MCP 服务，支持多格式文档批量导入、可追溯切分、相似度/混合检索，并能通过 REST 与 MCP 暴露统一查询能力。
- **主链路**：批量上传或连接数据源、对象存储暂存与任务登记、文档解析、OCR、结构化归一、稳定切分、向量化与索引、稠密+稀疏混合检索、过滤与重排、REST/MCP 返回片段、分数、来源与页码
- **硬约束**：用户尚未给出数据规模、并发、租户与部署环境；本报告按单组织、自托管、低于约一千万 chunk 的常见初期规模假设、优先选择允许商业二次开发的协议；自定义协议、无协议、多租户限制均显式标注、MCP 仅作为接入适配层，不承担文档状态、权限或索引生命周期、批量导入必须异步、幂等、可重试，并支持更新和删除同步
- **可接受技术栈**：Python、Docker/Compose、PostgreSQL、S3/MinIO、Qdrant 或 OpenSearch、FastMCP

## 检索与核验覆盖

| 类目 | 计划查询 | 已执行 | 成功 | 失败 | 候选 |
|---|---:|---:|---:|---:|---:|
| 端到端知识检索平台 | 18 | 18 | 18 | 0 | 378 |
| 文档解析与批量导入 | 18 | 18 | 17 | 1 | 322 |
| 相似度与混合检索 | 18 | 18 | 18 | 0 | 379 |
| 知识检索 MCP 服务 | 18 | 18 | 18 | 0 | 314 |

## 类目结论

| 类目 | 结论 | 首选 | 备选 | 理由 | 主要缺口 |
|---|---|---|---|---|---|
| 文档解析与批量导入 | 组合使用 | Docling | Unstructured（连接器与长尾格式）、Marker（高精 PDF）、MinerU（中文复杂版式/高吞吐）、Apache Tika（千种格式文本兜底） | Docling 的统一文档模型、版面/表格/OCR、MIT 协议与服务化形态最适合作为默认解析器；长尾数据源和特殊 PDF 通过路由式 fallback 处理。 | 仍需自研异步批处理、幂等、文件去重、解析器路由、失败重试、版本/删除同步。 |
| 相似度与混合检索 | 直接使用 | Qdrant | OpenSearch（已有全文检索团队或搜索优先）、pgvector（小规模且已用 PostgreSQL）、Milvus（十亿级向量/独立扩容）、Weaviate（偏集成式体验） | Qdrant 原生支持 dense、sparse、multivector、payload filter、hybrid query、REST/gRPC，部署和二次开发成本在候选中最均衡。 | 需要自行确定 embedding、稀疏向量/BM25、融合与 rerank 策略，并用真实问题集评估。 |
| 端到端知识检索平台 | 组合使用 | RAGFlow 用于快速业务 PoC；生产服务采用模块化组合 | AnythingLLM（小团队本地知识库）、Haystack（代码化管线）、LlamaIndex（连接器/检索组件） | RAGFlow 已覆盖解析、切分、检索、API、Agent/MCP，最快验证闭环；但其默认依赖 MySQL、Redis、MinIO、Elasticsearch/Infinity，运维面较大。只做检索服务时，模块化架构更可控。 | 全家桶仍不能替代企业 ACL、来源版本、删除同步、索引迁移和检索评估。 |
| 知识检索 MCP 服务 | 最小自研 | FastMCP 上实现薄适配层 | qdrant/mcp-server-qdrant（原型）、docling-project/docling-mcp（解析工具）、zilliztech/mcp-server-milvus（Milvus 路线） | 官方向量库 MCP 多暴露通用 store/find 或数据库管理工具，缺少文档 ACL、稳定 citation、版本与审计语义；使用 FastMCP 自建 3–5 个面向业务的只读工具更安全。 | 需要补鉴权、租户/ACL 过滤、限流、审计、错误码、工具契约与 prompt-injection 防护。 |

## 最小可跑通链路

1. **自研导入控制面 + PostgreSQL + S3/MinIO**：登记 source/document/version/job，保存原文件，生成 content hash 和稳定 document_id。；接法：上传接口只落盘并入队；worker 按状态机执行，失败可重试，更新和删除产生明确索引事件。；验收：一次导入 1,000 份混合文档；重复提交不重复建索引；失败可续跑；删除后原文、chunk 与向量一致清理。。
2. **Docling（默认）+ Unstructured/Marker 可插拔 fallback**：把 PDF、DOCX、PPTX、XLSX、HTML、图片等归一为结构化文档。；接法：统一输出 Document/Section/Table/Chunk 中间模型；保留 page、heading_path、bbox、source_uri、parser_version。；验收：真实样本集中正文顺序、表格和页码可追溯；扫描件触发 OCR；解析失败能按策略切换后备解析器。。
3. **Qdrant**：保存 dense/sparse 向量和 chunk payload，执行相似度、过滤及混合检索。；接法：以 chunk_id 幂等 upsert；payload 至少包含 tenant_id、document_id、version、ACL、page、heading、source_uri。；验收：支持 cosine top-k、metadata/ACL filter、dense+sparse 融合；更新和删除结果立即一致；在目标规模压测下达到约定 P95。。
4. **自研 Retrieval API**：封装 query rewrite、embedding、hybrid fusion、rerank 与 citation 拼装。；接法：REST 接口返回 chunk_id、score、rerank_score、document_id、page、source_uri、snippet；保留检索调试信息。；验收：用 100–300 条真实问答建立基线；hybrid+rerank 的 nDCG@10/Recall@10 明显优于 dense-only，并可复现实验版本。。
5. **FastMCP**：向 Claude/Codex/IDE/Agent 暴露稳定、最小权限的知识工具。；接法：首版仅提供 search_knowledge、fetch_document_chunks、list_sources；本地用 stdio，远程用 Streamable HTTP。；验收：所有查询强制 ACL；返回可引用来源；具备超时、限流、审计和只读模式；MCP 与 REST 检索结果一致。。

## 1. 端到端知识检索平台

| # | 仓库 | 预筛分 | Star / Fork | 默认分支最新 Commit | 最近 push | 最新 Release | 协议 | 核验 | 采用建议 |
|---:|---|---:|---:|---|---|---|---|---|---|
| 1 | [infiniflow/ragflow](https://github.com/infiniflow/ragflow) | — | 86.5k / 10.2k | 2026-08-01 | 2026-08-01 | 2026-07-07 | Apache-2.0 | verified | 直接使用 |
| 2 | [Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm) | — | 64.2k / 7.0k | 2026-07-30 | 2026-07-31 | 2026-06-25 | MIT | verified | 直接使用 |
| 3 | [langgenius/dify](https://github.com/langgenius/dify) | — | 151.0k / 23.8k | 2026-08-01 | 2026-08-01 | 2026-07-28 | Dify Open Source License | verified | 二次开发 |
| 4 | [deepset-ai/haystack](https://github.com/deepset-ai/haystack) | — | 26.1k / 3.0k | 2026-07-31 | 2026-08-01 | 2026-07-20 | Apache-2.0 | verified | 二次开发 |
| 5 | [run-llama/llama_index](https://github.com/run-llama/llama_index) | — | 51.3k / 7.8k | 2026-07-28 | 2026-08-01 | 2026-06-24 | MIT | verified | 二次开发 |
| 6 | [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) | — | 38.4k / 5.4k | 2026-08-01 | 2026-08-01 | 2026-07-31 | MIT | verified | 参考/抽取 |
| 7 | [microsoft/graphrag](https://github.com/microsoft/graphrag) | — | 35.1k / 3.7k | 2026-07-18 | 2026-07-26 | 2026-07-18 | MIT | verified | 参考/抽取 |

### 1. infiniflow/ragflow

- **摘要**：最快验证业务闭环的全家桶；默认以 Docker Compose 启动 MySQL、Redis、MinIO、Elasticsearch/Infinity 等依赖，生产运维面较大。
- **角色**：骨架
- **判断**：直接用于 PoC：已有 Docker、自托管、解析/切分/检索/API/MCP；2–5 天可跑通，但生产前需评估其多组件运维、权限模型和升级路径。
- **命中**：RAG platform、RAGFlow
- **风险**：依赖组件多，资源和升级成本高、只做检索服务时功能偏重
- **证据**：https://github.com/infiniflow/ragflow#readme、https://github.com/infiniflow/ragflow/blob/main/LICENSE、https://github.com/infiniflow/ragflow/releases

### 2. Mintplex-Labs/anything-llm

- **摘要**：适合小团队快速交付私有知识库 UI；它是应用产品而不是通用检索底座。
- **角色**：骨架
- **判断**：小团队可直接用：MIT、Docker、文档上传、引用、Developer API 和多用户均现成；若要独立 MCP 检索服务或复杂 ACL，建议只把它当 UI/验证层。
- **命中**：chat with documents、knowledge base
- **风险**：检索和文档模型受应用结构约束、多用户权限仅 Docker 版
- **证据**：https://github.com/Mintplex-Labs/anything-llm#readme、https://github.com/Mintplex-Labs/anything-llm/blob/master/LICENSE、https://github.com/Mintplex-Labs/anything-llm/releases

### 3. langgenius/dify

- **摘要**：适合在检索之上搭建工作流和应用，不建议把它作为唯一检索内核；许可证对多租户与前端标识有额外限制。
- **角色**：组件
- **判断**：组合使用：把 Dify 放在检索 API 之上做工作流/UI；其 RAG 可验证需求，但多租户 SaaS 与白标场景需先解决商业许可。
- **命中**：RAG pipeline、Dify
- **风险**：未经书面授权不得运行多租户环境、使用其前端时不能移除或修改 Logo/版权信息
- **证据**：https://github.com/langgenius/dify#readme、https://github.com/langgenius/dify/blob/main/LICENSE、https://github.com/langgenius/dify/releases

### 4. deepset-ai/haystack

- **摘要**：适合代码化、可测试的检索/RAG 管线；通过 Hayhooks 可暴露 REST 或 MCP，但仍需业务控制面。
- **角色**：骨架
- **判断**：二次开发首选框架之一：Apache-2.0、显式 pipeline、组件可替换；适合实现 Retrieval API，但导入任务、ACL、版本与审计仍需自研。
- **命中**：semantic search、RAG pipeline
- **风险**：框架升级会影响组件接口、不是现成的文档生命周期平台
- **证据**：https://github.com/deepset-ai/haystack#readme、https://github.com/deepset-ai/haystack/blob/main/LICENSE、https://github.com/deepset-ai/haystack/releases

### 5. run-llama/llama_index

- **摘要**：生态广、原型快，适合借用连接器和检索组件；README 中部分高级解析/索引能力属于 LlamaCloud 产品。
- **角色**：组件
- **判断**：选组件而非全盘绑定：可快速接数据源、向量库和 reranker；核心文档状态与检索 API 应保持自有，避免被庞大集成生态锁定。
- **命中**：document loaders、retriever
- **风险**：包和集成面很大，版本兼容成本需评估、LlamaParse/LlamaCloud 与 OSS 框架边界要分清
- **证据**：https://github.com/run-llama/llama_index#readme、https://github.com/run-llama/llama_index/blob/main/LICENSE、https://github.com/run-llama/llama_index/releases

### 6. HKUDS/LightRAG

- **摘要**：适合作为第二阶段 GraphRAG 实验，不应替代首版稳定的 chunk 级混合检索。
- **角色**：参考
- **判断**：第二阶段参考：当真实问题需要跨文档关系和全局摘要时再做 A/B；首版引入会显著增加索引延迟、成本和一致性难度。
- **命中**：GraphRAG、knowledge graph
- **风险**：实体抽取与图构建增加成本和更新复杂度、并非通用文档导入平台
- **证据**：https://github.com/HKUDS/LightRAG#readme、https://github.com/HKUDS/LightRAG/blob/main/LICENSE、https://github.com/HKUDS/LightRAG/releases

### 7. microsoft/graphrag

- **摘要**：研究和特定全局问答的高价值参考；索引成本、调参与数据更新复杂，不适合作为通用 MVP。
- **角色**：参考
- **判断**：仅在跨文档全局问题已被评测证明是核心需求时采用；否则先保留为算法参考，避免为少量问题承担全量图索引成本。
- **命中**：GraphRAG、RAG
- **风险**：索引阶段 LLM 成本高、增量更新与图谱一致性复杂
- **证据**：https://github.com/microsoft/graphrag#readme、https://github.com/microsoft/graphrag/blob/main/LICENSE、https://github.com/microsoft/graphrag/releases


## 2. 文档解析与批量导入

| # | 仓库 | 预筛分 | Star / Fork | 默认分支最新 Commit | 最近 push | 最新 Release | 协议 | 核验 | 采用建议 |
|---:|---|---:|---:|---|---|---|---|---|---|
| 1 | [docling-project/docling](https://github.com/docling-project/docling) | — | 64.1k / 4.5k | 2026-07-31 | 2026-07-31 | 2026-07-30 | MIT | verified | 直接使用 |
| 2 | [Unstructured-IO/unstructured](https://github.com/Unstructured-IO/unstructured) | — | 15.2k / 1.3k | 2026-07-26 | 2026-07-31 | 2026-07-31 | Apache-2.0 | verified | 二次开发 |
| 3 | [datalab-to/marker](https://github.com/datalab-to/marker) | — | 38.1k / 2.7k | 2026-07-20 | 2026-07-20 | 2026-07-20 | Apache-2.0 code / modified OpenRAIL-M models | verified | 二次开发 |
| 4 | [opendatalab/MinerU](https://github.com/opendatalab/MinerU) | — | 76.4k / 6.4k | 2026-07-10 | 2026-07-30 | 2026-07-10 | MinerU Open Source License | verified | 二次开发 |
| 5 | [apache/tika](https://github.com/apache/tika) | — | 3.9k / 951 | 2026-07-31 | 2026-07-31 | UNKNOWN | Apache-2.0 | verified | 参考/抽取 |

### 1. docling-project/docling

- **摘要**：默认解析器首选；支持 PDF/DOCX/PPTX/XLSX/HTML/EPUB/邮件/图片/音频字幕等，并可通过 docling-serve 服务化。
- **角色**：组件
- **判断**：直接接为默认解析器：MIT、格式广、结构化 JSON 保真、可服务化；预计 2–3 天接入 worker，批处理编排和文档版本需自研。
- **命中**：document-parser、Docling
- **风险**：模型权重需分别核验许可证、复杂 OCR/版面任务的 CPU 吞吐需实测
- **证据**：https://github.com/docling-project/docling#readme、https://github.com/docling-project/docling/blob/main/LICENSE、https://github.com/docling-project/docling/releases

### 2. Unstructured-IO/unstructured

- **摘要**：适合补长尾格式和数据源连接器；高级生产管线与部分能力指向商业产品，开源本地部署依赖较多。
- **角色**：组件
- **判断**：组合使用：优先拿连接器和长尾格式 partition；不要让其元素模型成为唯一内部 schema，避免后续替换解析器时迁移困难。
- **命中**：document loaders、Unstructured
- **风险**：PDF/图片本地解析依赖 Poppler/Tesseract 等、开源组件与商业 Pipelines 边界需确认
- **证据**：https://github.com/Unstructured-IO/unstructured#readme、https://github.com/Unstructured-IO/unstructured/blob/main/LICENSE.md、https://github.com/Unstructured-IO/unstructured/releases

### 3. datalab-to/marker

- **摘要**：适合困难 PDF 路由；代码 Apache-2.0，但默认模型权重的 OpenRAIL-M 商业使用有规模条件。
- **角色**：组件
- **判断**：按文档路由接入：对复杂 PDF/表格/公式有价值；先用 50–100 份困难样本与 Docling 对比，并在商用前核对模型权重许可。
- **命中**：PDF parser、OCR
- **风险**：模型权重商业许可对超过 500 万美元融资/收入的主体有限制、高精模式偏 GPU/VLM
- **证据**：https://github.com/datalab-to/marker#readme、https://github.com/datalab-to/marker/blob/master/LICENSE、https://github.com/datalab-to/marker/releases

### 4. opendatalab/MinerU

- **摘要**：复杂中文文档和高吞吐场景值得实测；许可证基于 Apache-2.0 但有商业阈值和在线服务署名义务。
- **角色**：组件
- **判断**：中文复杂版式备选：异步任务、多 GPU 路由和 Office 原生解析较完整；采用前必须接受署名义务并完成真实样本、吞吐和显存评估。
- **命中**：OCR、document parser
- **风险**：在线服务需显著标明使用 MinerU、MAU 超 1 亿或月收入超 2000 万美元需商业许可
- **证据**：https://github.com/opendatalab/MinerU#readme、https://github.com/opendatalab/MinerU/blob/master/LICENSE.md、https://github.com/opendatalab/MinerU/releases

### 5. apache/tika

- **摘要**：格式兜底很强，但不提供面向 RAG 的高级版面/表格语义；适合在 Docling 无法处理时抽纯文本。
- **角色**：组件
- **判断**：作为格式兜底：上千种文件类型检测/抽取成熟；不要用于要求精确页码、阅读顺序、表格和公式的主解析链。
- **命中**：Apache Tika、file type extraction
- **风险**：输出以文本/元数据为主，版面语义有限、Java 服务会增加一项运行时
- **证据**：https://github.com/apache/tika#readme、https://github.com/apache/tika/blob/main/LICENSE.txt、https://github.com/apache/tika/releases


## 3. 相似度与混合检索

| # | 仓库 | 预筛分 | Star / Fork | 默认分支最新 Commit | 最近 push | 最新 Release | 协议 | 核验 | 采用建议 |
|---:|---|---:|---:|---|---|---|---|---|---|
| 1 | [qdrant/qdrant](https://github.com/qdrant/qdrant) | — | 33.7k / 2.5k | 2026-07-17 | 2026-08-01 | 2026-07-17 | Apache-2.0 | verified | 直接使用 |
| 2 | [opensearch-project/OpenSearch](https://github.com/opensearch-project/OpenSearch) | — | 13.4k / 2.8k | 2026-07-31 | 2026-07-31 | 2026-06-09 | Apache-2.0 | verified | 二次开发 |
| 3 | [pgvector/pgvector](https://github.com/pgvector/pgvector) | — | 22.4k / 1.3k | 2026-07-29 | 2026-07-29 | UNKNOWN | PostgreSQL License | verified | 直接使用 |
| 4 | [weaviate/weaviate](https://github.com/weaviate/weaviate) | — | 16.7k / 1.4k | 2026-07-31 | 2026-08-01 | 2026-07-29 | BSD-3-Clause | verified | 二次开发 |
| 5 | [milvus-io/milvus](https://github.com/milvus-io/milvus) | — | 45.4k / 4.2k | 2026-07-31 | 2026-07-31 | 2026-07-29 | Apache-2.0 | verified | 二次开发 |
| 6 | [vespa-engine/vespa](https://github.com/vespa-engine/vespa) | — | 7.0k / 726 | 2026-08-01 | 2026-08-01 | 2026-07-28 | Apache-2.0 | verified | 参考/抽取 |

### 1. qdrant/qdrant

- **摘要**：新建中等规模知识检索服务的默认首选，功能覆盖与运维复杂度平衡最好。
- **角色**：组件
- **判断**：直接接：单节点 Docker 即可起步，REST/gRPC、过滤、dense+sparse、多向量都现成；预计 2–3 天完成 schema/upsert/search，生产再补高可用和备份。
- **命中**：vector-database、hybrid search、Qdrant
- **风险**：需要独立备份、快照和容量规划、BM25/稀疏向量与 rerank 策略仍需应用层定义
- **证据**：https://github.com/qdrant/qdrant#readme、https://github.com/qdrant/qdrant/blob/master/LICENSE、https://github.com/qdrant/qdrant/releases

### 2. opensearch-project/OpenSearch

- **摘要**：已有 Elasticsearch/OpenSearch 团队、关键词检索和复杂聚合占主导时更合适；新项目运维通常重于 Qdrant。
- **角色**：组件
- **判断**：条件采用：已有搜索基础设施或必须把 BM25、聚合、日志式搜索放在第一优先级时选；纯新建语义检索服务优先 Qdrant。
- **命中**：enterprise search、BM25、hybrid search
- **风险**：集群、JVM、索引参数与升级运维较重、向量检索功能分散在插件/版本能力中，需锁定版本验证
- **证据**：https://github.com/opensearch-project/OpenSearch#readme、https://github.com/opensearch-project/OpenSearch/blob/main/LICENSE.txt、https://github.com/opensearch-project/OpenSearch/releases

### 3. pgvector/pgvector

- **摘要**：小规模或强事务/Join 场景的最低运维方案；规模和高并发增长后要重点验证索引、过滤与 VACUUM。
- **角色**：组件
- **判断**：小规模直接用：若已有 PostgreSQL 且目标在百万级 chunk，能最小化组件数并保留事务/Join；上线前必须做带 ACL 过滤的真实压测。
- **命中**：vector similarity search、HNSW
- **风险**：向量与 OLTP 共库会争用资源、大规模 ANN、混合融合和分片能力弱于专用引擎
- **证据**：https://github.com/pgvector/pgvector#readme、https://github.com/pgvector/pgvector/blob/master/LICENSE、https://github.com/pgvector/pgvector/releases

### 4. weaviate/weaviate

- **摘要**：功能集成度高，适合希望数据库层包办向量化/混合检索的团队；配置与模块选择比 Qdrant 更复杂。
- **角色**：组件
- **判断**：功能集成优先时采用：BM25+向量+过滤单 API、多租户/RBAC 完整；若希望 embedding 与检索层清晰解耦，Qdrant 更简单。
- **命中**：hybrid search、Weaviate
- **风险**：集成模块多，需明确哪些模型在库内运行、官方 MCP 仓库无 LICENSE，不应直接复制商用
- **证据**：https://github.com/weaviate/weaviate#readme、https://github.com/weaviate/weaviate/blob/main/LICENSE、https://github.com/weaviate/weaviate/releases

### 5. milvus-io/milvus

- **摘要**：十亿级向量或需要 K8s 独立扩容时强；中小规模的组件数和运维成本高于 Qdrant/pgvector。
- **角色**：组件
- **判断**：规模驱动采用：十亿级向量、GPU/CPU 加速或计算存储独立扩容时选；MVP 可用 Milvus Lite/Standalone，但生产集群学习成本较高。
- **命中**：vector-database、BM25、Milvus
- **风险**：分布式形态组件多、运维复杂、中小规模可能过度设计
- **证据**：https://github.com/milvus-io/milvus#readme、https://github.com/milvus-io/milvus/blob/master/LICENSE、https://github.com/milvus-io/milvus/releases

### 6. vespa-engine/vespa

- **摘要**：搜索表达力和在线排序能力强，但团队学习曲线与运维成本最高，适合搜索是核心能力的成熟团队。
- **角色**：参考
- **判断**：只在复杂在线 ranking、超大规模 serving 和搜索团队能力都具备时评估；普通文档相似度检索不建议首选。
- **命中**：AI search platform、vector search
- **风险**：学习曲线陡、对普通知识库 MVP 过重
- **证据**：https://github.com/vespa-engine/vespa#readme、https://github.com/vespa-engine/vespa/blob/master/LICENSE、https://github.com/vespa-engine/vespa/releases


## 4. 知识检索 MCP 服务

| # | 仓库 | 预筛分 | Star / Fork | 默认分支最新 Commit | 最近 push | 最新 Release | 协议 | 核验 | 采用建议 |
|---:|---|---:|---:|---|---|---|---|---|---|
| 1 | [PrefectHQ/fastmcp](https://github.com/PrefectHQ/fastmcp) | — | 27.0k / 2.2k | 2026-07-30 | 2026-07-30 | 2026-07-27 | Apache-2.0 | verified | 直接使用 |
| 2 | [qdrant/mcp-server-qdrant](https://github.com/qdrant/mcp-server-qdrant) | — | 1.5k / 285 | 2026-06-10 | 2026-07-31 | 2025-12-10 | Apache-2.0 | verified | 二次开发 |
| 3 | [docling-project/docling-mcp](https://github.com/docling-project/docling-mcp) | — | 701 / 132 | 2026-07-31 | 2026-07-31 | 2026-07-31 | MIT | verified | 二次开发 |
| 4 | [zilliztech/mcp-server-milvus](https://github.com/zilliztech/mcp-server-milvus) | — | 237 / 71 | 2026-07-20 | 2026-07-20 | UNKNOWN | Apache-2.0 | verified | 二次开发 |
| 5 | [chroma-core/chroma-mcp](https://github.com/chroma-core/chroma-mcp) | — | 582 / 118 | 2025-09-17 | 2025-09-17 | 2025-08-14 | Apache-2.0 | verified | 参考/抽取 |
| 6 | [weaviate/mcp-server-weaviate](https://github.com/weaviate/mcp-server-weaviate) | — | 162 / 44 | 2026-05-26 | 2026-05-26 | UNKNOWN | UNKNOWN | verified | 不建议 |

### 1. PrefectHQ/fastmcp

- **摘要**：自建知识检索 MCP 薄层的首选；它只解决协议应用层，不解决检索、鉴权策略和文档生命周期。
- **角色**：骨架
- **判断**：直接用于薄适配层：Python 函数即可暴露 tools/resources/prompts；预计 1–2 天实现首版，但认证、ACL、审计和业务错误语义要自己补。
- **命中**：FastMCP、Model Context Protocol
- **风险**：版本演进快，升级前要跑协议兼容测试、企业级网关/治理是另一个产品层
- **证据**：https://github.com/PrefectHQ/fastmcp#readme、https://github.com/PrefectHQ/fastmcp/blob/main/LICENSE、https://github.com/PrefectHQ/fastmcp/releases

### 2. qdrant/mcp-server-qdrant

- **摘要**：原型验证很快，但接口以通用信息存取为主，缺少完整文档导入、ACL、citation 和版本语义。
- **角色**：参考
- **判断**：拿来跑原型或抄实现：配置后即可 store/find；生产应启用 QDRANT_READ_ONLY，并改造成 search/fetch/list_sources 的业务契约。
- **命中**：Qdrant MCP、semantic search MCP
- **风险**：工具契约过于通用、默认允许写入，生产知识库应启用只读并加 ACL
- **证据**：https://github.com/qdrant/mcp-server-qdrant#readme、https://github.com/qdrant/mcp-server-qdrant/blob/master/LICENSE、https://github.com/qdrant/mcp-server-qdrant/releases

### 3. docling-project/docling-mcp

- **摘要**：适合把文档转换暴露给 Agent，不等同于完整知识库 MCP；支持 stdio、SSE、Streamable HTTP。
- **角色**：组件
- **判断**：组合使用：可直接提供转换工具或参考其 transport/cache；知识库搜索、ACL、版本和 citation 仍由自有 MCP 层处理。
- **命中**：document MCP、Docling MCP
- **风险**：主要面向单文档转换/Agent 工具、大规模批量导入仍需任务控制面
- **证据**：https://github.com/docling-project/docling-mcp#readme、https://github.com/docling-project/docling-mcp/blob/main/LICENSE、https://github.com/docling-project/docling-mcp/releases

### 4. zilliztech/mcp-server-milvus

- **摘要**：Milvus 技术路线的可用适配层；数据库管理工具面较宽，生产应做只读裁剪和权限隔离。
- **角色**：组件
- **判断**：仅在选择 Milvus 时采用：搜索/查询工具较全且有 Streamable HTTP；生产前裁剪写入和集合管理工具，并补业务 ACL/citation。
- **命中**：Milvus MCP、vector database MCP
- **风险**：工具面包含数据库管理能力、社区规模较小且暂无 Release
- **证据**：https://github.com/zilliztech/mcp-server-milvus#readme、https://github.com/zilliztech/mcp-server-milvus/blob/main/LICENSE

### 5. chroma-core/chroma-mcp

- **摘要**：功能完整、适合本地原型，但活跃度弱于本批其他官方适配层；数据库管理工具不宜全部暴露给模型。
- **角色**：参考
- **判断**：本地原型可参考：collection/query/filter 工具齐全；新生产系统不建议因此选 Chroma，且需隐藏 delete/create 等高风险工具。
- **命中**：Chroma MCP、semantic search MCP
- **风险**：近一年提交/发布弱于 FastMCP、Docling MCP 和 Milvus MCP、暴露删除/集合管理工具有误操作风险
- **证据**：https://github.com/chroma-core/chroma-mcp#readme、https://github.com/chroma-core/chroma-mcp/blob/main/LICENSE、https://github.com/chroma-core/chroma-mcp/releases

### 6. weaviate/mcp-server-weaviate

- **摘要**：仓库仍活跃但没有 LICENSE；在未获得授权前只可研究接口，不能作为商用可复制组件。
- **角色**：参考
- **判断**：不直接采用：无开源许可证是商用硬风险；可阅读接口设计，但应使用 FastMCP 自建或等待官方明确授权。
- **命中**：Weaviate MCP
- **风险**：仓库无 LICENSE，默认版权保留、暂无 Release
- **证据**：https://github.com/weaviate/mcp-server-weaviate、https://github.com/weaviate/mcp-server-weaviate/commits/main

## 确需自研的缺口

- 导入任务控制面：source/document/version/job 状态机、幂等、重试、断点、配额。
- 统一文档与 chunk schema：稳定 ID、页码/标题层级/bbox、解析器和 embedding 版本、来源血缘。
- 租户与 ACL：索引时写入权限，查询时强制过滤；不能依赖 LLM 或 MCP 客户端自觉传参。
- 更新与删除同步：原文件、元数据、chunk、向量和缓存必须同生命周期。
- 检索策略与评估：dense/sparse、RRF/加权融合、reranker、阈值、去重与真实评测集。
- MCP 产品契约：面向业务的只读工具、认证授权、限流审计、错误语义和 prompt-injection 防护。
- 可观测性：每次解析和查询记录版本、延迟、命中、失败原因与成本，支持回放。

## 关键词矩阵

**端到端知识检索平台**

- `literal`：knowledge retrieval system、document knowledge base、enterprise search
- `jargon`：RAG platform、retrieval augmented generation、AI knowledge base、document QA
- `tech`：retriever reranker、semantic search embeddings、BM25 vector search
- `projects`：RAGFlow、Dify、Haystack、LlamaIndex、AnythingLLM
- `topics`：rag、knowledge-base、semantic-search
- `adjacent`：question answering、document intelligence、citation retrieval
- `zh`：知识检索系统、企业知识库、文档问答、RAG 知识库

**文档解析与批量导入**

- `literal`：document ingestion pipeline、document parsing、bulk document import
- `jargon`：ETL for LLM、document loaders、multimodal document extraction、chunking pipeline
- `tech`：PDF DOCX PPTX HTML OCR parser、layout analysis、table extraction、Apache Tika
- `projects`：Unstructured、Docling、Marker、MinerU
- `topics`：document-parser、document-ai、data-ingestion、ocr
- `adjacent`：web crawler、file watcher、metadata extraction
- `zh`：文档解析、批量导入、PDF解析、多格式文档

**相似度与混合检索**

- `literal`：vector similarity search、hybrid search engine、semantic document search
- `jargon`：dense sparse retrieval、late interaction retrieval、neural search
- `tech`：HNSW、BM25、reciprocal rank fusion、reranker、ColBERT
- `projects`：Qdrant、Weaviate、Milvus、OpenSearch、Vespa、pgvector
- `topics`：vector-database、vector-search、hybrid-search
- `adjacent`：cross encoder reranking、metadata filtering、retrieval evaluation
- `zh`：向量检索、混合检索、相似度查询、全文检索

**知识检索 MCP 服务**

- `literal`：knowledge base MCP server、document search MCP server、RAG MCP server
- `jargon`：MCP retrieval tool、semantic search MCP、vector database MCP server
- `tech`：Model Context Protocol、FastMCP、stdio、SSE、streamable HTTP
- `projects`：Qdrant MCP、Docling MCP、Milvus MCP、Chroma MCP
- `topics`：mcp-server、model-context-protocol、mcp
- `adjacent`：agent tools、AI assistant integration、knowledge graph MCP
- `zh`：知识库MCP服务、MCP检索、MCP向量数据库


## 二次过滤

schema v2 始终保留唯一的全量候选池；过滤只更新分类关系的 `selected` 和 `rank`。

```bash
python3 scripts/rank.py --in out/ranked.json --filter "关键词" --out out/r2.json
python3 scripts/report.py --in out/r2.json --outdir out --name github-scout-filtered
```

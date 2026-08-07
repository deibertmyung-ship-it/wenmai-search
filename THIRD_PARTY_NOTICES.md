# 第三方代码归属

本项目以 MIT 许可证发布（见 `LICENSE`）。下列组成部分来自其他项目，保留其原有许可证。

## noplag-engine

- **上游项目**：`noplag-engine`
- **许可证**：Apache License 2.0 —— 全文见 [`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`](third_party/noplag-engine/LICENSE-APACHE-2.0.txt)
- **固定提交**：`005da60faad21bf52702997d73583b78d8905d22`（2026-08-07，`Merge branch 'fix-language-code-mapping'`）
- **移植依据**：[ADR-0001](knowledge-service/docs/adr/0001-selectively-port-noplag-into-kbsvc.md)

本项目**选择性移植**了 noplag-engine 的算法实现，而非整包引入。移植文件均已按本项目架构改写（同步 SQLAlchemy、依赖注入、去除框架耦合），因此**不能直接 `git pull` 升级**，必须重新做差异审查与回归测试。

每个移植文件的头部都注明了上游路径、上述固定提交、Apache-2.0 以及 `Modified for kbsvc`。

### 移植范围

| 上游文件 | 本项目位置 |
| --- | --- |
| `src/noplag_engine/fingerprinting/winnowing.py` | `knowledge-service/src/kbsvc/plagiarism/fingerprinting/winnowing.py` |
| `src/noplag_engine/chunking/sliding.py` | `knowledge-service/src/kbsvc/plagiarism/chunking/sliding.py` |
| `src/noplag_engine/alignment/seed_extend.py` | `knowledge-service/src/kbsvc/plagiarism/alignment/seed_extend.py` |
| `src/noplag_engine/intervals.py` | `knowledge-service/src/kbsvc/plagiarism/intervals.py` |
| `src/noplag_engine/ingestion/language.py` | `knowledge-service/src/kbsvc/plagiarism/language.py` |
| `src/noplag_engine/retrieval/fingerprint_df.py` | `knowledge-service/src/kbsvc/plagiarism/retrieval/fingerprint_df.py` |
| `src/noplag_engine/retrieval/stop_list.py` | `knowledge-service/src/kbsvc/plagiarism/retrieval/stop_list.py` |
| `src/noplag_engine/retrieval/l1_winnowing.py` | `knowledge-service/src/kbsvc/plagiarism/retrieval/l1_winnowing.py` |
| `src/noplag_engine/workflows/check.py` | `knowledge-service/src/kbsvc/plagiarism/workflows/check.py` |
| `src/noplag_engine/workflows/corpus.py` | `knowledge-service/src/kbsvc/plagiarism/workflows/corpus.py` |
| `src/noplag_engine/workflows/progress.py` | `knowledge-service/src/kbsvc/plagiarism/workflows/progress.py` |

上游的 API 层、数据库层、迁移、上传与对象存储、demo、脚本与部署文件**未被移植**。

### 测试基线

`knowledge-service/tests/plagiarism/` 下的算法特征测试，其固定输入/输出向量迁移自上游同名测试
（`tests/test_winnowing.py`、`test_chunking.py`、`test_intervals.py`、`test_alignment.py`、
`test_stop_list.py`、`test_l1_retrieval.py`），用于证明适配过程没有改变算法语义。
这些测试同样受 Apache-2.0 覆盖。

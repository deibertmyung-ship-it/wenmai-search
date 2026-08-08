# 中文短文本查重漏检修复 Implementation Plan

> **For agentic workers:** 按 Task 顺序实施；每个 Task 先写失败测试，再写最小实现。未经用户确认，不修改业务代码。

**Goal:** 修复中文、尤其古汉语短文本被 30/50 全局阈值过滤的问题；统一候选指纹与最终对齐的规范化语义；让过短输入、空结果和历史报告都可解释，同时保持英文现有召回门槛不变。

**Architecture:** 在 `kbsvc.plagiarism` 内新增一个深模块式的匹配策略层：一次检查先解析实际语言和匹配配置，再用同一套可逆规范化结果驱动指纹与对齐。中文常规通道使用 12/20；仅中文 12–19 个有效字符启用严格整段精确匹配；非中文继续使用 30/50。`algorithm_config_hash` 继续表示语料投影兼容性，新增 `matcher_version` 与 `matcher_config` 保存每次运行时实际采用的语言、阈值和规范化版本。

**Tech Stack:** Python 3.11、SQLAlchemy、PostgreSQL、FastAPI/Pydantic、Flask/Jinja、pytest、Docker Compose。

## 需求解释与不可变约束

- 用户所写“1219 字”按 **12–19 个规范化后的有效字符**理解。
- 中文匹配策略：`min_seed_len=12`、`min_passage_len=20`。
- 非中文匹配策略：继续使用现有 `min_seed_len=30`、`min_passage_len=50`。
- 中文 12–19 有效字符只允许“整段精确匹配”通道；不能用普通容错扩展，也不能因为局部命中就上报。
- 少于 12 个有效字符的文本不进入检测队列，返回明确、结构化的 422 错误；worker 仍需保留同样的防御性检查，覆盖文档模式。
- “有效字符”必须由统一规范化器计算，不能使用 `str.strip()` 或原文长度代替。
- 报告坐标永远指向原始待检文本和原始来源文本，不能返回规范化字符串的坐标。
- 英文迁移基线测试继续固定 30/50，不能为了中文修复改动上游英文算法语义。
- 纯标点、分隔线和引用标记清理后若不足 12 个有效字符，必须被判为输入过短，而不是“检查完成且无来源”。
- 完整报告无命中时只写“没有找到达到当前检测阈值的重复段落”，不能声称绝对不存在重复。
- 本计划修复逐字/轻编辑查重，不包含语义向量查重；语义方案见 `knowledge-service/docs/research/2026-08-08-semantic-plagiarism-detection.md`。

## 核心接口与数据设计

### 匹配策略

在 `knowledge-service/src/kbsvc/plagiarism/matching_policy.py` 建议定义：

```python
@dataclass(frozen=True)
class MatchPolicy:
    matcher_version: str
    resolved_language: str
    profile: Literal["zh", "generic"]
    min_effective_chars: int
    min_seed_len: int
    min_passage_len: int
    short_exact_enabled: bool
    short_exact_min_score: float
    normalizer_version: str


def resolve_match_policy(
    text: str,
    declared_language: str,
    settings: Settings,
) -> MatchPolicy: ...
```

`declared_language != "auto"` 时尊重调用者；自动模式不能只依赖 `langdetect`，短古文容易误判，应先用汉字占比识别中文，再回退现有 `detect_language()`。中文判断必须统一用于查询与语料投影。

### 可逆规范化

在 `knowledge-service/src/kbsvc/plagiarism/normalization.py` 建议定义：

```python
@dataclass(frozen=True)
class NormalizedText:
    text: str
    original_starts: tuple[int, ...]
    original_ends: tuple[int, ...]

    def original_span(self, start: int, end: int) -> tuple[int, int]: ...


def normalize_with_offsets(text: str, *, profile: str) -> NormalizedText: ...
```

中文 profile 执行：NFKC、大小写折叠、去引用标记、忽略 Unicode 空白、忽略约定的中文/英文标点和引号。generic profile 保持现有英文行为：NFKC、去引用标记、lower、空白折叠；不得把英文单词无条件粘连。

映射规则必须处理 NFKC 一对多展开：每个规范化字符记录它来自的原始半开区间；规范化区间 `[start, end)` 映射回 `original_starts[start]` 到 `original_ends[end-1]`。删除字符不生成映射项。

### 运行快照

`plag_check` 新增两个非索引列：

- `matcher_version VARCHAR(64) NOT NULL DEFAULT ''`
- `matcher_config JSON NOT NULL DEFAULT '{}'`

`matcher_config` 至少保存：

```json
{
  "resolved_language": "zh-cn",
  "profile": "zh",
  "min_effective_chars": 12,
  "effective_chars": 24,
  "min_seed_len": 12,
  "min_passage_len": 20,
  "short_exact_enabled": true,
  "short_exact_min_score": 0.99,
  "normalizer_version": "plag-normalizer-v2"
}
```

`algorithm_config_hash` 仍只负责判断语料投影是否兼容；运行时阈值不再混入该哈希。规范化规则会改变存储的指纹，因此 `plagiarism_algorithm_config_hash` 的格式版本必须从 v1 升为 v2，并包含 `normalizer_version`。

## Task 0：锁定失败场景与当前英文基线

**Files:**

- Modify: `knowledge-service/tests/plagiarism/test_alignment.py`
- Modify: `knowledge-service/tests/plagiarism/test_check_flow.py`
- Modify: `knowledge-service/tests/plagiarism/test_api.py`
- Modify: `knowledge-service/tests/plagiarism/test_winnowing.py`
- Modify: `knowledge-web/tests/test_plagiarism.py`

**Steps:**

- [ ] 增加 24 字古文真实文本的失败测试，要求命中正确来源、`matched_chars=24`，两侧 offset 可切回原文。
- [ ] 增加 108 字与 188 字、多句均短于 30 字的真实古文流程测试，要求所有真实短句都进入合并覆盖区间，不得只命中一部分。
- [ ] 增加同主题但没有连续 12 字规范化相同内容的古文负例，要求零来源。
- [ ] 增加纯标点、空白、引用标记测试，要求 422 过短错误而非完成空报告。
- [ ] 固定英文迁移基线：29 字 seed 不命中、49 字 passage 不上报，30/50 行为不变。
- [ ] 增加空结果模板测试，先固定新文案。

**Gate:** 测试应先因当前 30/50 阈值、原始文本对齐和旧文案而失败；英文基线在修改前后均通过。

## Task 1：实现语言解析和独立阈值策略

**Files:**

- Create: `knowledge-service/src/kbsvc/plagiarism/matching_policy.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/language.py`
- Modify: `knowledge-service/src/kbsvc/config.py`
- Modify: `knowledge-service/.env.example`
- Modify: `knowledge-service/deploy/docker-compose.yml`
- Create: `knowledge-service/tests/plagiarism/test_matching_policy.py`
- Modify: `knowledge-service/tests/test_config.py`

**Configuration:**

```text
KB_PLAG_MIN_SEED_LEN=30
KB_PLAG_MIN_PASSAGE_LEN=50
KB_PLAG_ZH_MIN_SEED_LEN=12
KB_PLAG_ZH_MIN_PASSAGE_LEN=20
KB_PLAG_MIN_EFFECTIVE_CHARS=12
KB_PLAG_SHORT_EXACT_MIN_SCORE=0.99
```

**Steps:**

- [ ] 保留现有两个配置项作为 generic/英文阈值，新增中文与短文本配置。
- [ ] 增加正数、比例及关系校验：`min_effective_chars <= zh_min_seed_len < zh_min_passage_len`。
- [ ] 用汉字占比解决短古文自动语言识别不稳定；显式 `language` 优先。
- [ ] `resolve_match_policy()` 返回不可变对象，runner 后续不再直接散读多个 settings 字段。
- [ ] 测试 `zh`、`zh-cn`、`zh-tw`、auto 古文、英文和混合文本。

**Gate:** 24 字古文解析为中文 12/20；英文始终解析为 30/50。

## Task 2：统一规范化并建立可逆偏移映射

**Files:**

- Create: `knowledge-service/src/kbsvc/plagiarism/normalization.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/fingerprinting/winnowing.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/fingerprinting/__init__.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/alignment/seed_extend.py`
- Create: `knowledge-service/tests/plagiarism/test_normalization.py`
- Modify: `knowledge-service/tests/plagiarism/test_winnowing.py`
- Modify: `knowledge-service/tests/plagiarism/test_alignment.py`

**Steps:**

- [ ] 先实现并测试 `NormalizedText.original_span()`，覆盖空结果、边界、NFKC 展开和删除字符。
- [ ] 中文测试覆盖换行、连续空格、`，。；：！？、“”‘’《》〈〉` 及 `[12]`、`[citation needed]` 等引用标记差异。
- [ ] 将 `fingerprint()` 内部规范化委托给新模块；保留一个明确的 generic 默认值，使既有英文迁移向量不被意外改写。
- [ ] 对齐入口接收规范化字符串，输出前通过两侧映射恢复原始 offset；不要在 runner 中用字符串搜索猜坐标。
- [ ] 断言 `original_text[start:end]` 确实包含报告展示的原始命中内容。
- [ ] 纯标点中文规范化后为空；普通英文单词边界仍存在。

**Gate:** 指纹、seed、extend、score 使用同一规范化表示；报告仍使用原文坐标。

## Task 3：升级语料投影兼容版本并验证重建路径

**Files:**

- Modify: `knowledge-service/src/kbsvc/config.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/projection.py`
- Modify: `knowledge-service/tests/plagiarism/test_projection.py`
- Modify: `knowledge-service/tests/plagiarism/test_postgres_repository.py`
- Modify: `knowledge-service/docs/04-runbook.md`

**Steps:**

- [ ] 将投影格式版本提升到 v2，并把规范化版本纳入 `algorithm_config_hash`。
- [ ] ProjectionBuilder 使用投影语言对应的 profile 生成指纹；保存的 chunk 文本和 char offset 继续保持原样。
- [ ] 测试仅修改 12/20、30/50、0.99 不改变投影 hash；修改 normalizer version 必须改变 hash。
- [ ] 测试 v1 与 v2 投影不会混用，重建过程中旧报告仍按旧 snapshot 可读。
- [ ] 在 runbook 明确：本次需要全量重建 plagiarism projection 和 fingerprint DF；不删除 `plag_corpus_chunk`，不执行 `DROP INDEX`/`REINDEX`，GIN 会随新行自动维护。

**Gate:** 新检查在 v2 覆盖率未达到 100% 前被 corpus readiness 拒绝，不得降级到旧投影并产生假阴性。

## Task 4：实现中文常规通道与 12–19 字严格精确通道

**Files:**

- Modify: `knowledge-service/src/kbsvc/plagiarism/alignment/seed_extend.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/runner.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/repository.py`（仅当短通道需要独立候选查询）
- Modify: `knowledge-service/tests/plagiarism/test_alignment.py`
- Modify: `knowledge-service/tests/plagiarism/test_check_flow.py`

**Algorithm:**

- 中文有效长度 `>=20`：正常 seed-and-extend，实际参数 12/20。
- 中文有效长度 `12..19`：不进入容错 extend；在候选规范化文本中查找完整规范化查询串。
- 上报条件同时满足：查询覆盖率 `1.0`、规范化 score `>=0.99`、命中区间具备判别性。
- 非中文：只运行现有 30/50 常规通道，不启用中文短句通道。

**Steps:**

- [ ] 短句候选召回不要因 DF stop-list 把唯一指纹全部删掉；短通道可以使用未过滤 fingerprint probe，但必须保留 top-k、ACL、tenant、snapshot 和排除自身文档约束。
- [ ] 候选验证必须做完整字符串相等/包含，不能以“共享一个 fingerprint”作为最终结果。
- [ ] 同一 candidate 中多个精确位置可以分别产出，随后沿用现有 dedupe；设置每候选结果上限防止重复字符造成输出爆炸。
- [ ] `match_type` 明确记录 `verbatim` 或 `short_exact`，不得混成将来的 semantic match。
- [ ] 24、108、188 字走正常中文通道；12–19 字只走严格通道；11 字直接被拒。
- [ ] 无关古文、固定套语和纯标点不得上报。

**Gate:** 用户给出的 24 字原文稳定命中；英文 30/50 测试完全不变。

## Task 5：保存 matcher 版本与实际阈值，完成数据库增量升级

**Files:**

- Modify: `knowledge-service/src/kbsvc/plagiarism/models.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/schema.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/runner.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/types.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/service.py`
- Modify: `knowledge-service/src/kbsvc/api/plagiarism_schemas.py`
- Modify: `knowledge-service/tests/plagiarism/test_postgres_repository.py`
- Modify: `knowledge-service/tests/plagiarism/test_api.py`

**Database constraint:** `PlagiarismBase.metadata.create_all()` 不会给已有 `plag_check` 增加列。

**Steps:**

- [ ] 在 plagiarism schema 中加入专用 additive-column 列表与幂等升级函数，模式参考 `kbsvc.db.session._apply_additive_columns()`。
- [ ] `init_plagiarism_schema()` 在 `create_all()` 后执行升级；既有行得到安全默认值，不做全表 JSON 回填。
- [ ] `verify_plagiarism_schema()` 增加 `missing_columns`，缺列时 readiness 必须为 false。
- [ ] 增加“旧表已有数据，init 后补列且旧行仍存在”的 PostgreSQL 测试。
- [ ] 文本模式在创建检查时冻结策略；文档模式在首次成功解析正文后冻结策略。runner 若发现已有 `matcher_version`/`matcher_config` 必须复用，不得因部署期间环境变量变化让同一个 check 的重试改用另一组阈值。
- [ ] `CheckReport`、`ReportOut` 和 `ReportOut.of()` 返回这两个字段；历史行返回空版本和 `{}`。
- [ ] 不为这两个列创建索引：报告按 `plag_check.id` 查询，不存在按 matcher 配置筛选的在线访问路径。

**Gate:** 任一新报告能解释“为什么该检查用 12/20 或 30/50”；旧报告保持可读。

## Task 6：过短输入和无结果文案

**Files:**

- Modify: `knowledge-service/src/kbsvc/plagiarism/service.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/runner.py`
- Modify: `knowledge-service/src/kbsvc/api/routers/plagiarism.py`（仅如错误映射需要）
- Modify: `knowledge-service/docs/03-api.md`
- Modify: `knowledge-web/kbweb/views/plagiarism.py`
- Modify: `knowledge-web/kbweb/templates/partials/_check_report.html`
- Modify: `knowledge-web/tests/test_plagiarism.py`
- Modify: `knowledge-web/tests/e2e/stub_backend.py`

**API contract:**

```json
{
  "code": "plagiarism_text_too_short",
  "message": "有效文本少于 12 个字符，无法可靠查重",
  "detail": {"effective_chars": 7, "minimum": 12}
}
```

**Steps:**

- [ ] 文本模式在创建 job 前计算有效长度并返回 422；不得占用并发槽，也不得生成一条看似成功的历史检查。
- [ ] 文档模式在 runner 解析冻结文本后执行同一检查；使用结构化失败原因，详情页明确显示“文本过短，无法可靠查重”，不能渲染零来源结论。
- [ ] kbweb `_explain()` 单独处理 `plagiarism_text_too_short`，不要归入模糊的“输入不合法”。
- [ ] 完整且可检测但无来源时，将模板文案改为“没有找到达到当前检测阈值的重复段落。”
- [ ] partial/time-cap 报告继续强调未检查部分不代表没有重复。
- [ ] E2E FakeKbClient 和 stub report 增加 matcher 字段及过短错误场景。

**Gate:** 纯标点和 11 个有效字符显示过短提示；24 字无命中时显示阈值限定文案。

## Task 7：全量回归、真实语料验收与上线操作

**Files:**

- Modify: `knowledge-service/docs/02-data-model.md`
- Modify: `knowledge-service/docs/04-runbook.md`
- Modify: `knowledge-service/docs/05-performance.md`
- Modify: `knowledge-service/deploy/docker-compose.yml`
- Modify: `knowledge-service/.env.example`

**Automated verification:**

```powershell
cd F:\wen_mai_search\knowledge-service
pytest tests/plagiarism/test_matching_policy.py tests/plagiarism/test_normalization.py -q
pytest tests/plagiarism/test_winnowing.py tests/plagiarism/test_alignment.py -q
pytest tests/plagiarism/test_projection.py tests/plagiarism/test_postgres_repository.py -q
pytest tests/plagiarism/test_check_flow.py tests/plagiarism/test_api.py -q
ruff check src tests
pytest -q

cd F:\wen_mai_search\knowledge-web
pytest tests/test_plagiarism.py tests/test_report.py -q
ruff check kbweb tests
pytest -q
```

**Deployment sequence:**

```powershell
cd F:\wen_mai_search\knowledge-service

# 1. 部署包含新 ORM/DDL 升级代码的镜像，先关闭公开查重接口。
#    保持索引 worker 可用。
kbsvc plagiarism init

# 2. 新规范化规则改变指纹，登记全部 v2 投影重建任务。
kbsvc plagiarism rebuild

# 3. 由常驻 worker 处理，或维护窗口内排空。
kbsvc plagiarism-worker --once

# 4. 所有投影完成后重算 document frequency，并 ANALYZE GIN 所在表。
kbsvc plagiarism rebuild-df
kbsvc plagiarism status

# 5. 确认 schema ready、pending=0、failed=0、coverage=100%，再开放接口。
```

Docker 部署必须重新 build/recreate `knowledge-service` API 与 plagiarism worker，使新代码和新增环境变量进入容器。PostgreSQL 数据卷保留；不要删除 volume，不要手工删除 GIN 索引。

**Manual acceptance:**

- [ ] 输入“人禀天地、命属阴阳、生居覆载之内、尽在五行之中。”，来源为《命理正宗--张神峰》，命中区间覆盖整句。
- [ ] 在待检文中加入换行、空格、中文引号或不同标点，仍命中且高亮坐标正确。
- [ ] 108/188 字多短句文本命中所有应命中的连续区间，来源数不包含待检文本自身。
- [ ] 无关古文不命中；纯标点和少于 12 有效字符被明确拒绝。
- [ ] 英文 29 字 seed 和 49 字 passage 不上报；满足 30/50 的英文基线仍命中。
- [ ] 报告 API 返回 matcher 版本、实际语言、有效字符数、实际 12/20 或 30/50。
- [ ] 无命中报告使用限定文案；时间截断报告仍使用不完整警告。

## 索引与回滚说明

### 本次为什么需要投影重建

单纯把中文查询时阈值改成 12/20 不需要重建索引；但本方案还要求指纹与对齐共同忽略中文空白、标点和引用标记。存量 `plag_corpus_chunk.fingerprints` 是按旧规范化生成的，如果查询使用新规则而语料仍是旧规则，会再次出现候选召回与对齐不一致。因此必须生成 v2 投影。

### 不需要做什么

- 不需要 `DROP INDEX`。
- 不需要 PostgreSQL `REINDEX`。
- 不需要删除 `plag_corpus_chunk` 或数据库 volume。
- 不需要重建通用 Qdrant 向量索引。
- 不需要重建知识检索的 Tantivy 索引。

### 回滚

- 代码回滚后恢复旧 matcher/normalizer 版本和旧配置 hash。
- v1 投影在保留期内仍存在，可重新开放旧版本；不要在新版本验收前运行清理任务删除 retired projection。
- `matcher_version`/`matcher_config` 是纯增量列，回滚代码可以忽略它们，无需删除列。
- 若 v2 召回或误报指标不合格，保持 `KB_PLAG_ENABLED=false`，旧报告仍可读取。

## 风险与控制

| 风险 | 级别 | 控制措施 |
|---|---:|---|
| 短古文被 `langdetect` 误判而仍走 30/50 | 高 | 汉字占比优先、显式语言优先，并保存 resolved language |
| 查询采用新规范化、存量指纹仍是旧规范化 | 高 | normalizer 进入 projection hash，v2 未覆盖 100% 前拒绝新检查 |
| 删除标点后常见套语误报 | 高 | 12–19 只允许整段覆盖 + score≥0.99；保留判别性过滤和 top-k |
| 规范化坐标直接写入报告造成高亮漂移 | 高 | `NormalizedText` 双侧 offset map；测试用原文切片反查 |
| `create_all` 不补已有列 | 高 | plagiarism 专用 additive migration + missing_columns readiness |
| 误把阈值变化加入 projection hash导致以后频繁全量重建 | 中 | 只把 normalizer/分块/fingerprint 格式放入 hash；实际阈值只存 matcher snapshot |
| 短通道绕过 stop-list 后候选过多 | 中 | 独立 top-k、严格完整验证、每候选 occurrence 上限、性能计数 |
| 英文召回行为被中文规范化连带改变 | 中 | language profile 分离，保留英文迁移向量与 30/50 门禁 |

## 完成定义

- 六条用户要求都有自动化测试和至少一条端到端覆盖。
- 24、108、188 字真实古文测试通过，英文迁移基线无变化。
- 规范化前后两侧 offset 均能准确映射回原文。
- 过短输入不创建成功检查，无结果文案不作绝对结论。
- 新报告持久化并返回 matcher 版本和实际阈值。
- schema 升级对已有 `plag_check` 行无破坏，readiness 能识别缺列。
- v2 投影重建、DF 重算、Docker 重建与回滚步骤已写入 runbook 并实际演练。

## 实施状态（2026-08-09）

- [x] Task 0–2：失败场景、语言策略、统一规范化与可逆偏移。
- [x] Task 3–5：v2 投影兼容性、短文本严格通道、matcher 快照与增量迁移。
- [x] Task 6：过短输入 422、文案、报告字段与 E2E stub。
- [x] Task 7：回归测试、runbook、数据模型/API 文档、混合语言召回修复。

PostgreSQL 集成测试和 Docker/生产重建仍需在目标部署环境执行；本地单元测试与前端查重测试已通过。

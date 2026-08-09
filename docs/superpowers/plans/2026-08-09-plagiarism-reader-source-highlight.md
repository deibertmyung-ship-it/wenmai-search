# 查重来源精确定位与高亮 Implementation Plan

> **For agentic workers:** 按 Task 顺序实施；每个 Task 先写失败测试，再写最小实现。未经用户确认，不修改业务代码，不执行数据库迁移或容器重建。

**Goal:** 用户在查重报告中点击某一处“到书里看”后，打开检测时的来源书籍版本，直接滚动到该命中段落，并精确高亮来源文字；普通阅读器和检索结果现有的 `focus=<chunk ordinal>` 深链保持兼容。

**Architecture:** 报告已经保存来源 `document_id`、检测时 `version` 以及全文半开字符区间 `[source_start, source_end)`。后端新增独立的 `PassageLocator` 深模块，将“历史版本 + 全文字符区间”一次解析为有限的阅读窗口、锚点 chunk 和每个 chunk 的局部高亮区间。Web 端只负责传递契约、把安全文本片段渲染为 `<mark>`，不逐页扫描整本书，也不重新运行查重算法。

**Tech Stack:** Python 3.11、SQLAlchemy、PostgreSQL、FastAPI/Pydantic、Flask/Jinja、原生 JavaScript、pytest、Playwright、Docker Compose。

## 需求解释与不可变约束

- “到书里看”属于每一条 `passage`，链接必须携带该 passage 的 `source_start/source_end`，不能只定位到来源书籍首页，也不能只取 source 的第一处命中。
- 必须使用报告冻结的 `source.version`。书籍重新导入后，不能拿当前版本解释历史报告坐标。
- 坐标统一为 Python/数据库现有语义的半开区间 `[start, end)`；不得在前端重新做文本搜索来猜位置。
- 首屏必须服务端渲染目标窗口和 `<mark>`，保证关闭 JavaScript 仍可直接定位；JavaScript 只负责继续加载后文和可选的滚动增强。
- 普通 `/read/<document_id>?from=...` 与搜索结果使用的 `?focus=<ordinal>#c<ordinal>` 行为必须保持不变。
- Jinja 不拼接可信 HTML，不使用 `|safe`；正文按普通字符串自动转义，仅由模板结构生成 `<mark>`，防止来源正文中的 HTML/脚本被执行。
- 用户仍需通过现有 document tenant/ACL 校验；无权访问、文档已删除或历史版本已清理时，只显示明确的不可用提示，不泄露正文。
- v1 不改变查重算法、报告坐标、chunk 切分或语料投影；不重建 Qdrant、Tantivy、plagiarism projection、fingerprint DF 或 PostgreSQL 索引。

## 核心接口与数据契约

### 报告深链

```text
/read/{document_id}
  ?version={source.version}
  &hit_start={passage.source_start}
  &hit_end={passage.source_end}
  #match
```

只有 `version`、`hit_start`、`hit_end` 三个参数同时有效时才进入命中模式；缺失、非整数或 `end <= start` 不允许静默回退到当前版本并展示错误高亮。

### knowledge-service API

```http
GET /v1/documents/{document_id}/passage-window
    ?version=3&start=12580&end=12624&context=2
```

建议响应：

```json
{
  "document_id": "...",
  "version": 3,
  "version_id": "...",
  "source_start": 12580,
  "source_end": 12624,
  "anchor_ordinal": 37,
  "window_start_ordinal": 35,
  "next_ordinal": 41,
  "has_more": true,
  "exact": true,
  "chunks": [
    {
      "chunk_id": "...",
      "ordinal": 37,
      "text": "...",
      "char_start": 12490,
      "char_end": 12920,
      "heading_path": [],
      "highlights": [
        {"local_start": 90, "local_end": 134, "source_start": 12580, "source_end": 12624}
      ]
    }
  ]
}
```

成功响应只允许 `exact=true`。如果历史 chunks 无法完整、连续地解释报告区间，endpoint 返回 `409 passage_location_unavailable`；页面可以提供打开历史版本书籍首页的回退，但不能展示“部分高亮”冒充精确结果。

### PassageLocator 内部接口

在 `knowledge-service/src/kbsvc/reading.py` 定义不可变返回类型和单一入口：

```python
def locate_passage(
    session: Session,
    *,
    tenant_id: str,
    document_id: str,
    version: int,
    source_start: int,
    source_end: int,
    context_chunks: int = 2,
) -> PassageWindow: ...
```

模块内部负责版本解析、区间查询、重叠去重、局部坐标换算、窗口组装和完整性判定；router 不复制这些规则。

## 定位算法

1. 用现有 `_load_document()` 等价的 tenant/ACL 条件取得文档，再按 `(document_id, version)` 取得不可变 `DocumentVersion`。
2. 校验 `0 <= source_start < source_end`、`context_chunks` 上限以及单次响应安全上限；超限返回结构化 422，不能退回当前版本。
3. 直接查询同一 `version_id` 下满足 `chunk.char_end > source_start AND chunk.char_start < source_end` 的相交 chunks；禁止从 ordinal 0 分页扫描到命中位置。
4. 按 ordinal 处理相交 chunks。每个全文字符只分配给第一个覆盖它的 chunk；相邻 chunks 有 overlap 时，从后续 chunk 扣除已覆盖区间，避免同一来源文字被重复高亮。
5. 将剩余全文区间换算为 `local_start/local_end`，并验证 `0 <= local_start < local_end <= len(chunk.text)`。完整性判断必须复现 `ProjectionBuilder._load_text()` 的坐标语义：chunks 之间被投影器补成空格的已知 gap 可以解释为不可见空白而不生成 `<mark>`，其余未覆盖字符一律返回 409；至少要存在一个真实高亮片段。
6. 用第一个非空高亮 chunk 作为 `anchor_ordinal`，再通过 `(version_id, ordinal)` 取前后各 `context_chunks` 个 chunk，合并为初始阅读窗口。
7. 若 passage 横跨多个 chunk，为每个 chunk 返回独立局部高亮区间；模板只在第一个 `<mark>` 上设置 `id="match"`。
8. 若没有任何可确认区间或覆盖不完整，返回稳定错误 `passage_location_unavailable`；Web 显示历史来源无法精确定位，并提供打开该历史版本书籍首页的回退链接。

## 错误契约

- `404 document_not_found`：文档不存在或按现有安全语义不可见。
- `404 version_not_found`：报告引用的历史版本已不存在。
- `422 invalid_passage_range`：负数、空区间、过大区间或 context 越界。
- `409 passage_location_unavailable`：版本存在，但历史 chunk 坐标无法定位任何可信正文。
- 上游暂时不可用/超时：Web 显示临时错误；不得改用当前版本，也不得把异常吞成普通空页面。

## Task 0：冻结现状、失败场景与契约

**Files:**

- Create: `knowledge-service/tests/test_passage_locator.py`
- Modify: `knowledge-service/tests/test_api_and_mcp.py`
- Modify: `knowledge-web/tests/test_views.py`
- Modify: `knowledge-web/tests/test_plagiarism.py`

**Steps:**

- [ ] 固定当前普通 reader：`from`、`focus`、`#c{ordinal}`、按 ordinal 加载后文的行为，作为迁移基线。
- [ ] 写报告链接失败测试，断言每个 passage 分别带上冻结 `version` 和自己的 `source_start/source_end`，fragment 为 `#match`。
- [ ] 写同 chunk、跨 chunk、chunk overlap、历史版本、首尾边界、无效区间、缺失版本、ACL 拒绝、含 `<script>` 正文的失败测试。
- [ ] 写深位置性能行为测试：locator 不能调用 `chunks_for_version()` 或从 ordinal 0 循环分页。
- [ ] 冻结上面的 API JSON 和错误 code；避免 service 与 web 分别猜字段名。

**Gate:** 新测试因 endpoint、locator、深链和 `<mark>` 尚不存在而失败；既有 reader/search 测试仍通过。

## Task 1：实现 PassageLocator 数据库定位模块

**Files:**

- Create: `knowledge-service/src/kbsvc/reading.py`
- Modify: `knowledge-service/src/kbsvc/db/repo.py`
- Modify: `knowledge-service/tests/test_passage_locator.py`

**Steps:**

- [ ] 在 `db/repo.py` 增加两个窄查询：按 `(version_id, char range)` 取相交 chunks；按 `(version_id, ordinal range)` 取上下文窗口。
- [ ] 在 `reading.py` 定义 `HighlightRange`、`PassageChunk`、`PassageWindow` 不可变 dataclass，以及 `locate_passage()`。
- [ ] 实现区间差集而非简单 clamp，保证 overlapping chunks 中一个全文字符只高亮一次。
- [ ] 以 `len(chunk.text)` 验证局部坐标和全文覆盖并集；坏数据只能导致 `unavailable`，不能偏移到相邻字符。
- [ ] 增加 projection gap 回归：跨越已知补空格间隙的 passage 仍能精确定位两侧正文；完全落在 gap 或无法解释的缺口返回 409。
- [ ] 限制 `context_chunks`（建议 0..10）和单次窗口 chunks（建议最多 200）；超过安全上限返回稳定错误，禁止无界内存响应。
- [ ] 测试 query count 为常数级，与命中 ordinal 无关。

**Gate:** locator 单元测试全部通过；10000 ordinal 的模拟目标不读取前 9999 个 chunks。

## Task 2：暴露 passage-window API

**Files:**

- Modify: `knowledge-service/src/kbsvc/api/schemas.py`
- Modify: `knowledge-service/src/kbsvc/api/routers/documents.py`
- Modify: `knowledge-service/tests/test_api_and_mcp.py`
- Modify: `knowledge-service/docs/03-api.md`

**Steps:**

- [ ] 新增 Pydantic 输出模型，明确局部与全文坐标，避免都叫 `start/end` 造成误用。
- [ ] 新增 `GET /v1/documents/{document_id}/passage-window`，只做参数解析、认证依赖、调用 locator 和 schema 映射。
- [ ] 沿用 documents router 的 tenant/ACL 规则；404 不暴露跨租户文档是否存在。
- [ ] 为错误 envelope 增加精确 code 断言；FastAPI 参数类型错误仍使用统一 validation envelope。
- [ ] 文档记录半开区间、历史版本语义、上限、`coverage` 和所有错误。

**Gate:** API 测试覆盖 exact、不可解析、跨 chunk、历史版本及 404/409/422；OpenAPI 能生成新 schema。

## Task 3：统一 documents 与查重的 ACL 读取语义

**Files:**

- Create: `knowledge-service/src/kbsvc/access.py`
- Modify: `knowledge-service/src/kbsvc/plagiarism/service.py`
- Modify: `knowledge-service/src/kbsvc/api/routers/documents.py`
- Modify: `knowledge-service/tests/test_api_and_mcp.py`
- Modify: `knowledge-service/tests/plagiarism/test_check_flow.py`

**Steps:**

- [ ] 将查重服务现有的 ACL 可见性规则提取为共享纯函数，documents router 与 plagiarism service 不再各自复制一套判断。
- [ ] 保持 principal ACL 为空时现有管理员/不限制语义；有 ACL 时只允许标签相交或明确 public 的文档。
- [ ] `get_document`、`get_chunks` 与新 `passage-window` 使用同一可见性检查，避免首次页面受控而 AJAX 续读泄露正文。
- [ ] 文档不存在、跨 tenant、被删除、ACL 被撤销统一使用现有 404 隐藏语义，不向客户端区分资源是否真实存在。
- [ ] 增加 ACL 撤销后的历史报告回归：报告仍可显示冻结摘要，但 reader 和 chunks 均不得返回来源正文。

**Gate:** 三个 documents 读取入口权限语义一致；管理员与 public 文档现有行为无回归。

## Task 4：扩展 Web 后端客户端契约

**Files:**

- Modify: `knowledge-web/kbweb/client.py`
- Modify: `knowledge-web/tests/test_plagiarism.py`
- Modify: `knowledge-web/tests/e2e/stub_backend.py`

**Steps:**

- [ ] 增加 `KbClient.get_passage_window(document_id, *, version, start, end, context)`，精确转发参数。
- [ ] FakeKbClient 实现相同方法；测试夹具能生成同 chunk、跨 chunk 和历史版本窗口。
- [ ] 403/404/409 与网络错误继续作为 `BackendError` 交给 reader route 分类，不在 client 中静默降级。
- [ ] 保留现有 `get_chunks(..., version=...)`；普通阅读和 AJAX 后续分页仍复用它。

**Gate:** 客户端测试断言请求路径和全部 query 参数，未知错误不被吞掉。

## Task 5：让阅读器支持冻结版本与命中模式

**Files:**

- Modify: `knowledge-web/kbweb/views/library.py`
- Modify: `knowledge-web/kbweb/views/api.py`
- Modify: `knowledge-web/kbweb/templates/reader.html`
- Modify: `knowledge-web/tests/test_views.py`

**Steps:**

- [ ] `library.read()` 严格解析 `version/hit_start/hit_end`；三者齐全时调用 passage-window，否则走原有 `from/focus` 分支。
- [ ] 命中模式使用窗口返回的 chunks、`anchor_ordinal`、`next_ordinal/has_more`，页面显示“检测时第 N 版”，避免用户误以为是最新版。
- [ ] 若历史版本或精确位置不可用，渲染专门提示和安全回退；绝不自动请求当前版本。
- [ ] `/api/chunks/<document_id>` 解析并转发 `version`；非法 version 被夹紧/拒绝的规则与 reader 一致。
- [ ] reader 的 `data-chunks-url`、继续加载、无 JavaScript 下一页链接都保留 `version`；离开命中窗口后的普通分页不再携带 `hit_start/hit_end`，避免反复跳回锚点。
- [ ] 继续通过原有 `focus` 回归，确保检索结果点击行为不变。

**Gate:** 历史报告始终读取历史版本；普通 reader/search 的现有测试零改语义通过。

## Task 6：安全渲染精确高亮

**Files:**

- Create: `knowledge-web/kbweb/reader.py`
- Create: `knowledge-web/tests/test_reader.py`
- Modify: `knowledge-web/kbweb/views/library.py`
- Modify: `knowledge-web/kbweb/templates/reader.html`

**Steps:**

- [ ] 在 `kbweb/reader.py` 实现纯函数，把一个 chunk 的局部 ranges 转成 `[{text, highlighted, anchor}]` 片段；校验、排序、合并相邻区间。
- [ ] 模板循环输出普通文本片段；高亮片段包在 `<mark class="reader__match">` 中，仅第一段加 `id="match"` 与可访问说明。
- [ ] 不生成 `Markup`、不调用 `|safe`、不使用 `innerHTML`；XSS 测试断言 `<script>` 只作为转义文本显示。
- [ ] 跨 chunk 的所有可信片段均高亮，只有第一个 mark 是 URL 锚点。
- [ ] locator 返回 409 时不渲染任何 `<mark>`，只显示“无法精确定位”与历史版本普通阅读回退。

**Gate:** 同 chunk、跨 chunk、重叠、空/坏 ranges、HTML 正文测试通过；关闭 JavaScript 后 `#match` 仍能落到命中位置。

## Task 7：改造查重报告“到书里看”链接

**Files:**

- Modify: `knowledge-web/kbweb/templates/partials/_check_report.html`
- Modify: `knowledge-web/tests/test_plagiarism.py`

**Steps:**

- [ ] 在 passage 循环内用该 passage 的 `source_start/source_end` 和父 source 的 `version` 生成深链。
- [ ] 保留来源标题链接指向普通书籍页面；只有“到书里看”按钮进入精确命中模式。
- [ ] 对缺少 version/offset 的旧报告禁用精确深链，显示“历史报告缺少定位信息”，不得拼出 `None` 或落到当前版本。
- [ ] 一个 source 多处命中时，逐一断言按钮指向不同区间。

**Gate:** 用户点击任意“第 N 处”的按钮，URL 与该 passage 完全对应。

## Task 8：阅读体验、样式与无 JavaScript 回退

**Files:**

- Modify: `knowledge-web/kbweb/static/css/app.css`
- Modify: `knowledge-web/kbweb/static/js/reader.js`
- Modify: `knowledge-web/kbweb/templates/reader.html`
- Modify: `knowledge-web/tests/e2e/test_interactions.py`
- Modify: `knowledge-web/tests/e2e/test_responsive_a11y.py`

**Steps:**

- [ ] 为 `.reader__match` 使用与现有纸张主题协调、明暗主题均达到可读对比度的底色；设置 `scroll-margin-top` 防止被固定导航遮挡。
- [ ] 页面加载时优先依赖原生 fragment；JS 只在需要时调用 `scrollIntoView({block: "center"})`，并尊重 `prefers-reduced-motion`。
- [ ] reader.js 继续用 `textContent` 创建动态 chunk；命中窗口已包含完整命中时，后续追加不再重复创建 mark。
- [ ] 键盘与读屏可识别命中区域，焦点不被强制抢走。
- [ ] 在 360px、桌面、暗色主题和无 JS 模式验收定位与高亮。

**Gate:** 目标 mark 首屏可见、不被导航遮挡；页面无横向溢出；无 JS 仍可用。

## Task 9：端到端与故障回归

**Files:**

- Modify: `knowledge-web/tests/e2e/stub_backend.py`
- Modify: `knowledge-web/tests/e2e/test_plagiarism_flow.py`
- Modify: `knowledge-web/tests/e2e/test_interactions.py`

**Steps:**

- [ ] 从查重报告展开第 2 处 passage，点击其按钮，断言阅读器打开正确 `version`，`#match` 位于 viewport 且高亮文本精确相等。
- [ ] 书籍已有新版本时，断言仍显示检测时旧版本的文字，不显示最新版同坐标内容。
- [ ] 覆盖跨 chunk 命中和 overlap 去重；拼接所有 marks 的文本后等于目标来源文字且不重复。
- [ ] 覆盖删除历史版本、ACL 被撤销、上游超时，断言页面给出对应提示且不泄露正文、不降级当前版本。
- [ ] 覆盖普通检索结果 `focus` 深链，防止新 query 参数解析破坏旧功能。

**Gate:** 新 E2E 全绿，既有 plagiarism、reader、search E2E 全绿。

## Task 10：性能、文档与容器上线

**Files:**

- Modify: `knowledge-service/docs/04-runbook.md`
- Modify: `knowledge-service/docs/05-performance.md`
- Modify: `knowledge-web/README.md`（仅当已有部署说明需要补充）

**Database decision:**

- v1 不新增索引。现有 `chunk.version_id` B-tree 可先把扫描范围缩到单一版本；上下文查询已有唯一索引 `(version_id, ordinal)`。
- 在真实最大书籍上记录 `EXPLAIN (ANALYZE, BUFFERS)` 和接口 P95。只有当 passage 相交查询稳定超过 50ms 或单版本 chunk 数量达到实测瓶颈，才另开迁移增加 `chunk(version_id, char_start)` 复合 B-tree。
- 如果后续加索引，使用独立的 `CREATE INDEX CONCURRENTLY IF NOT EXISTS` 运维迁移，不能塞进普通事务，也不是 `REINDEX`；上线前后都不删除现有索引。

**Verification:**

```powershell
cd F:\wen_mai_search\knowledge-service
python -m pytest -q tests/test_passage_locator.py tests/test_api_and_mcp.py
python -m pytest -q
python -m ruff check .

cd F:\wen_mai_search\knowledge-web
python -m pytest -q tests/test_reader.py tests/test_views.py tests/test_plagiarism.py
python -m pytest -q
python -m ruff check .
python -m pytest -q tests/e2e/test_plagiarism_flow.py tests/e2e/test_interactions.py tests/e2e/test_responsive_a11y.py
```

**Container rollout:**

```powershell
cd F:\wen_mai_search\knowledge-service\deploy
docker compose build api web
docker compose up -d --no-deps --force-recreate api web
docker compose ps
docker compose logs --tail=100 api web
```

- [ ] 先在宿主机完成单元/集成/E2E 测试，再构建 `api` 与 `web`；worker 未改，无需重建。
- [ ] 访问健康检查、普通阅读器、搜索深链、查重报告深链各做一次 smoke test。
- [ ] 确认无数据库 DDL、无 corpus rebuild、无索引重建任务，回退只需恢复旧 `api/web` 镜像。

**Gate:** 容器内新 endpoint 可访问；真实报告按钮能定位并高亮；日志无 4xx/5xx 异常；回退路径已验证。

## 推荐实施顺序与提交边界

1. Task 0：仅测试与契约。
2. Task 1–3：service locator、API 与共享 ACL，形成可独立验证的后端提交。
3. Task 4–7：web client、reader、安全渲染、报告深链，形成前端功能提交。
4. Task 8–9：样式、无 JS 与 E2E，形成体验/回归提交。
5. Task 10：文档、性能证据与容器发布；不把试验性数据库索引混入本功能。

## 主要风险与规避

- **历史版本错位：** 链路任何一层漏传 `version` 都会产生“看似成功但高亮错文”；每层都有参数转发测试，并禁止回退当前版本。
- **chunk overlap 重复：** 不能简单给所有相交 chunk 都做交集；必须用全文区间差集分配并测试拼接结果。
- **坐标与文本长度不一致：** 不 clamp、不 fuzzy search；返回 `passage_location_unavailable` 明示数据质量问题。
- **ACL 规则分叉：** documents 与 plagiarism 共用一个纯函数，并同时覆盖初次 SSR 与后续 chunks 请求。
- **XSS：** 不把高亮后的字符串当 HTML；模板对每个文本片段自动转义。
- **深位置性能：** 禁止 `attach_excerpts()` 式从 ordinal 0 分页；只做 version/range 查询和 ordinal 窗口查询。
- **旧报告兼容：** 没有版本或 offset 时不伪造深链，仍允许用户打开普通书籍页。
- **数据库运维误操作：** 本功能不需要索引重建或语料重建；性能索引必须有 EXPLAIN 证据并走后续并发增量迁移。

## 完成定义

- 报告每个 passage 的按钮都能打开检测时来源版本并精确高亮对应原文。
- 同 chunk、跨 chunk、overlap、历史版本、坏坐标、无权限、无 JavaScript、XSS 场景都有自动化测试。
- 普通 reader、检索 `focus`、继续加载和响应式体验无回归。
- 定位查询与书中 ordinal 距离无关，不扫描整本书。
- 不执行数据库 schema 变更、索引重建或查重语料重建。

---

**实施前确认点：** 请用户确认本计划后，再从 Task 0 开始写失败测试；当前步骤只创建计划文档，不修改业务代码。

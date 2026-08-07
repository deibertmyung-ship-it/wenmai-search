# 设计：抄袭检测 Web 界面（kbweb）

- 日期：2026-08-08
- 状态：待实现
- 相关：`knowledge-service/docs/specs/2026-08-07-plagiarism-detection-backend.md`（其 Out of Scope 明确不含前端）、
  `knowledge-web/docs/01-frontend-design.md`、`knowledge-service/docs/adr/0001-selectively-port-noplag-into-kbsvc.md`

## 背景

抄袭检测后端已完成：`/v1/plagiarism/*` 七条端点、持久化事件表驱动的 SSE 进度、
七态生命周期。后端规格把「前端与 MCP 工具」列在 Out of Scope，所以界面是全新的一块。

kbweb 是 kbsvc 的表现层：Flask + Jinja SSR、原生 JS、无构建步骤，
且**「JS 全部失效时页面仍可用」是硬性要求**（`KBWEB_NOJS` 已是一等配置与测试维度）。
本设计的每一处交互都必须同时给出无脚本路径。

## 目标

给出一个**判定导向**的查重界面：用户提交稿件，得到一个总重复率与全文高亮，
并能逐处追到来源出处。

## 非目标（首版）

- 报告导出与下载
- 批量查重
- 重复率阈值红黄绿灯——阈值是机构政策，不是工具默认值
- 「排除引用」——后端没有引用识别能力
- 检测参数调节——后端已把「按请求覆盖算法参数」列为 Out of Scope
- 文件上传检测——后端 Out of Scope，需另立 ADR
- 左右栏滚动联动

## 约束

- kbweb 不直连任何存储，只经 HTTP 调 kbsvc；不引入前端状态存储
- 朱砂红 `oklch(48% 0.17 25)` 是语义色，只用于命中高亮，不作装饰
- 无脚本路径必须完整可用
- kbweb 跑在 waitress，`deploy/Dockerfile` 当前是裸 `waitress-serve`，即默认 4 线程

## 关键事实

实施前必须知道的六条，均已在代码中核实：

1. **`preview` 是查询侧文字，不是来源侧。** `runner.py:301` 为
   `preview = query_text[p.query_start:p.query_end][:plag_preview_chars]`。
   来源侧只有 `source_start`/`source_end` 偏移，没有文本。
2. **来源正文走既有文档接口。** `ChunkOut` 带 `char_start`/`char_end`，可按
   `source_start` 定位。后端规格原话：「检测报告只返回预览、来源正文仍走既有文档接口，
   不新增读取路径」。
3. **提交的稿件原文，HTTP 契约当前不返回。** `PlagCheck.query_text` 存在库里，
   但 `CheckOut` 注释写死 "Never carries the submitted text"，`ReportOut` 也无此字段。
   见下文「后端依赖」。
4. **`filters.py` 的 `highlight_segments` 不能复用。** 它第 45 行遇重叠区间即丢弃
   （`overlaps the previous hit; skip rather than double-mark`）。对检索高亮正确，
   对抄袭报告错误——重叠是常态且带信息。
5. **`COMPLETED_PARTIAL` 是刻意区分的状态。** `types.py` 注释：
   "must never be read as 'no plagiarism found'"。
6. **`plag_sse_max_seconds` 为 900 秒。** 一条 SSE 流独占一个 waitress 线程。

## 后端依赖

本设计需要后端一处改动，须与后端会话协调：

- `ReportOut` 增加 `query_text` 字段（或单开 `GET /checks/{id}/text`）
- 新建一条 ADR 记录为何放宽「报告不携带提交文本」

**理由**：该约束的本意针对来源侧——`models.py:268` 原话是
*"a report must not become a way to read source documents around the ACL check"*。
查询侧是提交者自己的稿件，且 `get_report` 已校验所有权，回显给创建者本人不构成
ACL 问题。判定导向的全文高亮没有底稿则无法成立。

未落地前，前端无法渲染全文高亮，报告页只能退化为命中片段列表。

## 架构

### 路由

新增 blueprint `kbweb/views/plagiarism.py`：

| 路由 | 方法 | 作用 |
|---|---|---|
| `/plagiarism` | GET | 提交页（textarea）+ 下方历史检测列表 |
| `/plagiarism` | POST | 建文本检测 → 302 到详情页 |
| `/plagiarism/documents/<document_id>` | POST | 从文档详情页发起 → 302 到详情页 |
| `/plagiarism/checks/<check_id>` | GET | 详情页：进度与报告共用一个 URL |
| `/plagiarism/checks/<check_id>/delete` | POST | 删除或取消 → 302 |
| `/plagiarism/checks/<check_id>/events` | GET | SSE 代理，仅 JS 路径使用 |

masthead 增加第五项「查重」，置于「任务」之前。

### 为什么详情页两态共用一个 URL

详情页按 `status` 分支：非终态渲染进度，终态渲染报告。

这是无脚本路径能成立的原因：`<meta http-equiv="refresh" content="5">` 刷的是同一个 URL，
检测跑完后同一次刷新自然变成报告页，无需任何跳转逻辑。拆成 `/progress` 与 `/report`
两个路由，无脚本路径就得自行判断何时跳转。

### 为什么提交页与历史列表合一

判定导向下用户进来就是要交稿，历史是回看用的次要内容，放同页下方即可。
也避免导航栏出现第六项。

## 组件与改动

### 新增文件

```
kbweb/views/plagiarism.py                    路由
kbweb/templates/plagiarism.html              提交 + 历史
kbweb/templates/check.html                   详情（二态）
kbweb/templates/partials/_check_progress.html
kbweb/templates/partials/_check_report.html
kbweb/static/js/check.js                     EventSource + 就地展开增强
```

### 改动既有文件

- `kbweb/client.py` — 增加七个方法，对应七条后端端点
- `kbweb/filters.py` — 增加 `coverage_segments`
- `kbweb/__init__.py` — 注册 blueprint
- `kbweb/templates/base.html` — 导航增加「查重」
- `kbweb/templates/document.html` — 增加「查重」按钮（POST 到文档检测端点）
- `kbweb/static/css/app.css` — 追加样式，不新建文件
- `knowledge-web/deploy/Dockerfile` — `waitress-serve` 增加 `--threads=16`

### `coverage_segments`（本次唯一的算法性代码）

```python
def coverage_segments(
    text: str, spans_by_source: list[tuple[int, int, int]]
) -> list[tuple[str, frozenset[int]]]:
    """把 text 切成 (片段, 命中它的来源序号集合) 序列。

    与 highlight_segments 的区别在于重叠：那里丢弃，这里必须保留并合并，
    因为一段文字同时命中多个来源本身就是报告要表达的信息。
    """
```

输入是 `(query_start, query_end, source_ordinal)` 三元组列表，
实现为区间边界扫描线。纯函数、无 I/O。

`source_ordinal` 为 **1-based**，与角标 ①②③ 和右栏清单序号同一套编号。

不修改 `highlight_segments`——两个场景语义不同，合并会把两边都搞糊。

## 关键界面决策

### 顶部判定条

整个页面唯一的大号数字，用衬线巨号（沿用检索页排名数字的 scale contrast）：

- `is_complete = true` → 「重复率 **23.4%**」+ 副行「3 处来源 · 已查完整篇」
- `is_complete = false` → 「已发现重复 **≥ 23.4%**」+ 强制朱砂横幅：
  「仅检查了 1,240 / 3,908 段（时间预算用尽）。未检查部分不代表没有重复。」

重复率取 `matched_chars / query_chars`。

**`≥` 不是修辞。** 判定导向的界面上一个干净的百分比就是一句结论；覆盖不全时它只是下界。
后端为此专门建了 `COMPLETED_PARTIAL` 状态，前端必须把这个区分传递到最显眼的位置。

### 主体两栏

- 左栏（宽）：稿件全文（`query_text`），经 `coverage_segments` 渲染，
  朱砂底纹 + 句末角标 ①②
- 右栏（窄，sticky）：来源清单，按 `matched_chars` 降序，
  每条为 序号 / 书名 / 命中字数 / score

角标序号与右栏清单序号一一对应。一段文字命中多个来源时并列多个角标。

**为何单色 + 角标而非每来源一色**：保住「朱砂 = 命中」的语义色承诺；
来源超过五个后色相不够用；对色盲不友好。古籍注疏本来就是角标这个语言。

### 对照卡

用 `<details>/<summary>`——展开是纯 HTML 行为，无脚本下照样可点开。
`check.js` 只做增强：点左栏高亮 → 打开右栏对应 details 并滚过去。

卡内两栏：「你的文字」取 `passage.preview`；「来源」取自
`/v1/documents/<source.document_id>/chunks`，按 `source_start`/`source_end`
用 `char_start`/`char_end` 定位。

**来源文字在服务端渲染报告时按来源文档各拉一次**（不是每 passage 一次），
请求数等于来源文档数，**上限 8**——取 `matched_chars` 降序的前 8 个来源文档，
与右栏清单同一排序；其余来源只给「到书里看 →」链接，不预取正文。

懒加载首屏更快，但会让无脚本下的对照卡只剩一半，不划算。

## 数据流

### 三条运行路径

```
JS 路径     EventSource → /plagiarism/checks/<id>/events
            → Flask httpx.stream() 转发 → kbsvc SSE
            终态事件到达 → location.reload()，报告由服务端渲染
无脚本      <meta http-equiv="refresh" content="5">，终态时不输出该标签
出错        SSE 连不上 → 服务端已渲染的状态仍在，退化为静态页
```

终态用 reload 收尾而非前端拼报告——与现有 `jobs.js` 收尾方式一致，
报告只有一处渲染逻辑。

### SSE 代理的三个必须项

缺任何一项，线程账算不平或续传失效：

1. Flask 侧单条流 **120 秒主动关闭**，靠 EventSource 原生重连续上
2. Flask 必须把 `Last-Event-ID` 请求头**透传**给 kbsvc，
   否则精确续传失效、每次重连重放整条流
3. `deploy/Dockerfile` 的 `waitress-serve` 加 `--threads=16`

后端 `plag_sse_max_seconds` 为 900 秒，waitress 默认 4 线程：
不截流则两个用户同时看进度即吃掉一半线程池。

### 提交与幂等

提交表单内放一个渲染时生成的隐藏 uuid，POST 时作为 `Idempotency-Key` 透传。
用户双击或提交后刷新会命中同一 key，后端返回同一个 check。

不做的话很容易撞满 `plag_max_active_checks_per_key = 2`，第三次提交直接报错。

### 详情页读取顺序

```
GET /v1/plagiarism/checks/<id>          → status
  非终态                                 → 渲染进度
  completed / completed_partial          → 再 GET .../report → 渲染报告
  failed / cancelled                     → 渲染终态说明
```

### 语料就绪

提交页先查 `GET /v1/plagiarism/corpus/status`：

- `is_ready = false` → 提交按钮禁用，说明「语料准备中 1,203 / 1,580」
- `total_documents = 0` → 说明「书库为空，先导入」

否则用户会提交一个必然查不出东西的检测。

### 取消与删除

同一个 POST 端点，按后端返回语义分流：

- 204 → flash「已删除」，回列表
- 202 → flash「已请求取消，正在停止」，留在详情页

## 错误处理

按 `error.code` 分流，四个需专门文案：

| code | HTTP | 前端处理 |
|---|---|---|
| `feature_unavailable` | 503 | 「当前部署未启用抄袭检测（需要 PostgreSQL）」，非通用后端错误页 |
| `plagiarism_concurrency_limit` | 429 | 「已有 N 个检测在跑」+ 指向同页历史列表去取消 |
| `validation_error` | 400 | 输入超 `plag_max_input_chars`（50 万字）。**服务端必须校验**，不能只靠 JS 字数提示 |
| `not_found` | 404 | 不存在与不属于你不区分——后端刻意如此，前端不要试图区分 |

kbsvc 不可达沿用既有 `BackendUnavailable` → `errors/backend_down.html`。

### 来源不可访问的降级

`get_report` 注释说明访问权可能在检测跑完后被撤销。
服务端拉来源 chunks 时，单个来源 404/403 → 该对照卡显示「来源已不可访问」，
其余照常渲染。一本书被删不能让整个报告 500。

## 测试计划

沿用现有四层：

### 1. `tests/test_views.py`（respx mock kbsvc）

- 六条路由的正常路径
- 详情页两态分支：七种 `status` 各渲染什么
- 四个错误码各自的分流与文案
- `Idempotency-Key` 确实被透传
- 语料未就绪时提交按钮为 disabled

### 2. `tests/test_nojs.py`

现有 no-JS 契约断言扩到新路由：

- `KBWEB_NOJS=1` 下新页面不得出现任何 `<script>`
- meta refresh **非终态必须有、终态必须没有**（否则报告页会永远自刷）
- `<details>` 对照卡在无脚本下可展开

### 3. `tests/test_filters.py`

`coverage_segments` 纯函数单测：

- 完全重叠、部分重叠、相邻不重叠、区间包含
- 区间越界、`spans` 为空、`text` 为空
- 角标编号与右栏清单序号一致
- 多来源片段的来源集合正确合并

### 4. `tests/e2e/`

`stub_backend.py` 补 plagiarism 端点，含一段假 SSE 流。跑通：
提交 → 看到进度阶段推进 → 终态 → 报告高亮可见 → 展开对照卡。

## 实施顺序

1. 后端加 `query_text` 与对应 ADR（**阻塞第 6 步**，与后端会话协调）
2. `coverage_segments` + 单测（纯函数）
3. `client.py` 七个方法
4. 路由骨架 + 提交页 + 进度页，无脚本路径先跑通
5. `test_nojs.py` 与 `test_views.py`
6. 报告页：顶部判定条 + 来源清单 + 全文高亮 ← **依赖第 1 步**
7. SSE 代理 + `check.js` + Dockerfile 线程数
8. 对照卡的来源文字拉取与降级
9. e2e

第 2–5 步与第 7 步不依赖后端改动，可与后端会话并行推进。
若第 1 步迟迟不落地，第 6 步可先只做顶部判定条与来源清单（两者只用现有
`ReportOut` 字段），把全文高亮留到 `query_text` 到位后补上。

## 遗留

- 全文对照的滚动联动
- 报告导出
- `feature_unavailable` 部署下导航项是否隐藏——首版进页面才知道，
  不做启动探测

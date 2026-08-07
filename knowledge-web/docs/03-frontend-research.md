# kbweb 前端选型调研

> 调研目标：为已成型的 kbweb 前端寻找可借鉴的开源项目与模板。
>
> 所有候选均经 `gh api repos/OWNER/NAME` 实测验证（star / license / 最后 push / archived 状态），
> 验证日期 **2026-08-07**。未验证或无法定性授权的项目已在文中标注。

## 0. 前提：这次调研的定位是补漏，不是换方案

kbweb 不是待填的骨架，而是一套已做完取舍的成品：135 行 `tokens.css` + 511 行 `app.css`，
语义色分工（朱砂只给引用与命中、靛青只给度量）、双字体家族、6:1 排名字号比、
`--measure-read: 42em` 都已定下；`search.html` 里连「复选框未勾选时浏览器不提交」这种坑
都用隐藏 `f=1` 标记处理并有测试守着。

因此本文不推荐任何「整体套用」的模板。三路独立调研得出同一结论——

- **Flask boilerplate 这一类基本死绝**：主流模板三个已归档、四个停更两年以上
- **Astro / Hugo editorial 主题生态**：`--topic=astro-theme` 排序前 20 全是 Tailwind 落地页模板
- **RAG 系统自带前端**：不是 license 有雷就是体量失控

kbweb 已有的 create_app 工厂 + `views / templates / static / client.py` 分层，
比清单里任何模板都更贴合它自己的形态。

---

## 1. 唯一真正的对标物：whoogle-search

模板全军覆没，但有一个**同构的活体应用**比任何模板都贴合。

| 项目 | star | license | 最后 push |
|---|---|---|---|
| [`benbusby/whoogle-search`](https://github.com/benbusby/whoogle-search) | 11594 | MIT | 2026-08-04 |

自托管元搜索引擎：Flask SSR + httpx 打上游 + 自己不存数据，把结果渲染成 HTML。
与 kbweb 的形态几乎一一对应。

**逐文件对应关系：**

| whoogle 文件 | kbweb 对应 | 可借鉴什么 |
|---|---|---|
| `app/services/http_client.py` | `client.py` | `httpx.Client` 薄封装：跨请求复用连接、重试、TTLCache、HTTP/2 可降级 |
| `app/services/provider.py` | — | `close_all_clients()`，Flask teardown 时统一收敛客户端生命周期 |
| `app/models/config.py` + `test/conftest.py` | `config.py` + `tests/` | **把 `nojs` 做成一等配置维度，并在 conftest 里随机成 0/1 跑测试** |
| `test/mock_google.py` | `tests/` | 把上游响应做成可参数化的 fixture 工厂 |

**最该抄的是 `nojs` 那条。** kbweb 的「JS 全挂时页面仍可检索/翻页/上传」目前是硬性要求，
但只有 e2e 里一个降级测试守着。whoogle 的做法能把它变成全套单元测试的一个维度，
从人工验收变成 pytest 断言。

**别学的地方**：whoogle 是模块级 `app = Flask(__name__)`，非工厂模式。kbweb 现有写法更好。
另外它的 `filter.py` 是 BeautifulSoup 洗上游 HTML，与 kbweb 的 Jinja `filters.py` 名字撞车
但完全不是一回事。

**其余活体参考**（仅参考级，不建议逐行借鉴）：

| 项目 | star | license | 最后 push | 说明 |
|---|---|---|---|---|
| [`CTFd/CTFd`](https://github.com/CTFd/CTFd) | 6776 | Apache-2.0 | 2026-08-07 | 插件化 Flask + Jinja 主题系统 |
| [`miguelgrinberg/microblog`](https://github.com/miguelgrinberg/microblog) | 4775 | MIT | 2025-04-06 | 工厂 + blueprint 规范写法，但是数据库驱动的 CRUD |
| [`simple-login/app`](https://github.com/simple-login/app) | 6892 | **AGPL-3.0** | 2026-07-31 | 只能看不能抄 |

### 1.1 已排除的 Flask 模板

| 项目 | 排除原因 |
|---|---|
| `hack4impact/flask-base` | **archived**，最后 push 2024-03-21 |
| `nuvic/flask_for_startups` | **archived** |
| `MaxHalford/flask-boilerplate` | **archived**，最后 push 2018-01-15 |
| `realpython/flask-boilerplate` | 1579★，push 2023-11-03（33 个月） |
| `karec/cookiecutter-flask-restful` | 812★，push 2023-04-27（40 个月） |
| `realpython/cookiecutter-flask-skeleton` | 437★，push 2021-08-05 |
| `cookiecutter-flask/cookiecutter-flask` | 4724★ / MIT / 活跃，但核心卖点是 webpack 资产打包——正是 kbweb 拒绝的 |

---

## 2. 最硬的缺口：字体是设备彩票

这一条是白盒验证的结论，不依赖外部佐证。

`kbweb/static/css/tokens.css:33` 的字体栈是纯本地依赖：

```css
--font-serif: "Songti SC", "SimSun", "Noto Serif CJK SC",
              "Source Han Serif SC", "STSong", Georgia, serif;
```

- macOS 落 Songti SC
- Windows 落 SimSun（点阵感重，与「善本」意象相悖）
- Linux 大概率一路 fallback 到 Georgia，再由浏览器兜底任意 CJK 字体

**整个「古籍善本」方向建立在字体上，而字体现在不受控。** 子集化后自托管是唯一确定解。

| 组件 | 项目 | star | license | 最后 push |
|---|---|---|---|---|
| 字体切割 | [`KonghaYao/cn-font-split`](https://github.com/KonghaYao/cn-font-split) | 1227 | Apache-2.0 | 2026-06-12 |
| 楷体 | [`lxgw/LxgwWenKai`](https://github.com/lxgw/LxgwWenKai) 霞鹜文楷 | 25413 | **OFL-1.1** | 2026-07-30 |
| 宋体 | Noto Serif CJK SC | — | OFL | — |

`cn-font-split` 把 CJK 字体按 unicode range 切成上百片 woff2，浏览器只下载页面实际用到的分片。
是**构建期一次性成本**，不是运行时依赖，可接进 `deploy/`。
现成分片产物可参考 `SunsetMkt/HarmonyOS_Sans_SC_Webfont_Splitted`。

**顺带兑现一个只做了一半的取向。** 设计文档写的是「字体对比而非字号对比」，
但目前只有 serif / sans 一层对比。宋楷分工是古籍排版的刚需——正文宋、标题与注文楷。
建议把 `--font-serif` 拆成：

```css
--font-song: /* 正文 */
--font-kai:  /* 标题、注文 */
```

霞鹜文楷是 OFL-1.1，可自由嵌入商用；而系统楷体（KaiTi / STKai）授权不清且跨平台缺失。
配合 Noto Serif CJK SC（OFL）就有了确定的宋 + 楷双家族。

繁体版：[`lxgw/LxgwWenkaiTC`](https://github.com/lxgw/LxgwWenkaiTC)（816★ / OFL-1.1 / 2026-07-31）。

---

## 3. CJK 排版：唯一能直接拿 CSS 的是 heti

| 项目 | star | license | 最后 push |
|---|---|---|---|
| [`sivan/heti`](https://github.com/sivan/heti) | 6714 | MIT | 2025-08-31 |

「赫蹏」，中文内容展示排版增强。README 明写「暂时没什么想做的了」——是功能完成，不是弃坑。

**按文件挑规则抄进 `app.css`，不要整包引**（它是 SCSS + BEM `.heti--x` 修饰类体系，
全量引入会覆盖你的 reset 与字号体系）：

| 文件 | 内容 |
|---|---|
| `lib/modifiers/ancient.scss` | 古文/诗词版式。古文段落首行缩进 2em、诗节不缩进居中；`heti-meta`（作者/朝代）桌面端**悬挂且不占空间**，不破坏标题视觉居中 |
| `lib/modifiers/annotation.scss` | 行间注版式。着重号 `text-emphasis: filled circle` + `text-emphasis-position: under right`，`non-cjk-block` 内关闭 |
| `lib/modifiers/writing-mode.scss` | 竖排 |
| `lib/modifiers/column.scss` | 多栏（双行小注的基础） |
| `lib/fonts/_song.scss` / `_kai.scss` / `_hei.scss` | 分家族字体栈，比单一 `--font-serif` 分得细 |

**一个可直接采纳的实测约束**：`annotation.scss` 注释里留了数字——
**着重号最小可用行高 1.7，ruby 最小可用行高 2.0**。
kbweb 现在 `--leading-read: 1.95`，正好卡在能上着重号、ruby 会挤的位置。

> ⚠️ **不要引 `heti-addon.js`**。它做中西文加空格与全角标点挤压，是 DOM 改写，
> 会与命中高亮 `<mark>` 的渲染打架。

### 3.1 标点挤压改用 CSS 原生特性

[`sofish/typo.css`](https://github.com/sofish/typo.css)（4542★ / NOASSERTION / 2026-06-25）
的 `docs/modern-chinese-typography.md` 在跟进 `text-spacing-trim`——CSS 原生标点挤压，
Chrome 已发布。配 `@supports` 降级即可，零运行时成本，也不碰 DOM。比 heti 的 JS 方案干净。

> ⚠️ typo.css 的 license GitHub 未识别（NOASSERTION），用前需读 LICENSE 原文。
> 建议只取 `docs/` 里的排版结论与用法，不引代码。

### 3.2 标点禁则：一条被下调的判断

> **更正**：本文初稿写的是「kbweb 现在的 CSS 没有任何标点禁则处理」。这句话不成立。
> 浏览器默认就按 UAX #14 断行，「。」「，」本来就不会出现在行首；`app.css` 的
> `.result__snippet` 与 `.chunk__text` 也早已带了 `hanging-punctuation: allow-end`，
> 那正是行尾悬挂标点。真实的增量比初稿声称的小得多。

CSS 侧还能补的只有 `line-break: strict`——它收紧 `auto` 仍然允许、而禁则不允许的剩余
情形（小假名、〜、‐ 等）。**是收紧，不是修复。**

| 项目 | star | license | 最后 push |
|---|---|---|---|
| [`w3c/clreq`](https://github.com/w3c/clreq) | 800 | W3C 文档许可 | 2026-07-29 |

《中文排版需求》官方规范，中英简繁三语。**这是规范查询，不是依赖引入。**
需要它来定：标点避头尾、行首行尾标点处理、中西文混排四分之一空、着重号位置、
竖排标点旋转与位移、夹注与双行小注的排法。

### 3.3 Han.css：与 heti 二选一，选 heti

[`ethantw/Han`](https://github.com/ethantw/Han)（2536★ / MIT / 2026-06-23）竖排与繁体分流
比 heti 更完整（大量 `:lang()` 驱动的简繁分流），但设计极强势——会接管你的字号、行高、
字体全部，且 `han.js` 把标点包成 `<h-char>` 大改 DOM。
**只有在要做繁体竖排时才回头看它。**

---

## 4. 检索调试抽屉：确实没有先例

README 称调试抽屉是「整个前端最不像模板的地方」。这不是自我感觉良好。

针对性代码搜索（`"reciprocal rank fusion"` in tsx、`rrf_score` in ts、`denseScore sparseScore`、
`sparse_rank dense_rank`、`bm25_rank vector_rank`、`semantic_rank keyword_rank`、
`vectorRank keywordRank`）**全部零命中，或只命中后端算法实现与营销官网文案**。

没有任何现成项目同时做到「左右并列 dense/sparse 两路原始命中 + 每条显示各路名次 +
RRF 融合后名次与各自贡献」。

原因不难理解：会做 RRF 的项目要么把它藏在后端（`rrf.ts` 之类工具文件遍地都是），
要么前端只暴露融合后的最终分。把融合过程摊开给人看，只有做检索平台的人才有动机，
而这些人的 UI 通常是 Kibana / Dashboards 插件，不面向终端用户。

**但它由三块已有实现拼成，license 全部可安全移植：**

| kbweb 需要的部分 | 抄哪里 | star | license | 最后 push | 差在哪 |
|---|---|---|---|---|---|
| 左右并列两组结果 + 同一条目 SVG 连线 + 名次差着色 | [`opensearch-project/dashboards-search-relevance`](https://github.com/opensearch-project/dashboards-search-relevance) → `public/components/query_compare/search_result/visual_comparison/` | 28 | Apache-2.0 | 2026-08-06 | 对比的是「查询配置 A vs B」而非「dense 路 vs sparse 路」；EUI + OSD 平台绑定，必须移植 |
| `trace: true` 请求 flag + 分阶段响应契约 | [`Shadow-Weave/HMS`](https://github.com/Shadow-Weave/HMS) → `interface/console/src/components/search-debug-view.tsx` | 549 | MIT | 2026-08-05 | 纵向堆叠非左右并列；只给融合后分数，**不回溯各路原始名次**；50KB 单文件 |
| 每条命中并排显示 融合分 / 词法分 / 向量分 | [`infiniflow/ragflow`](https://github.com/infiniflow/ragflow) → `web/src/pages/dataset/testing/testing-result.tsx` | 87022 | Apache-2.0 | 2026-08-07 | 显示的是**相似度分数**不是**名次**；单栏列表；无融合过程展开 |
| 把最终分拆解成加权项 | [`o19s/splainer-search`](https://github.com/o19s/splainer-search) | 28 | Apache-2.0 | 2026-04-24 | 只懂 Lucene `explain` 树，完全不懂 RRF；AngularJS |

### 4.1 建议的移植路径

**OpenSearch 那个只有 28 星，但它把最难的部分已经写好且有单测。** 具体文件：

- `visual_comparison.tsx`（24KB）——左右两栏并列，每条带 `rank: index + 1`；
  `calculateStatistics()` 算 `inBoth / onlyInResult1 / onlyInResult2 / unchanged / improved / worsened`；
  按 `item1.rank > item2.rank` 给条目上色
- `connection_lines.tsx` + `result1ItemsRef` / `result2ItemsRef`——用 ref 测量两栏中同一文档的
  DOM 位置，**画 SVG 连线**，线色编码名次变化
- `item_detail_hover_pane.tsx`、`highlight_text.tsx`
- `__tests__/` 五个测试文件（含 `connection_lines.test.tsx`），可当行为规格读

把「配置 A vs 配置 B」换成「dense 路 vs sparse 路」，把 rank delta 换成 RRF 贡献即可。

**数据契约抄 HMS**：请求侧只在 payload 加 `trace: true`，响应带 `data.trace`，
分 `PARALLEL RETRIEVAL → RRF Fusion → Reranked` 三阶段。它的 `viewMode: "results" | "trace" | "json"`
三态切换也值得参考。注意其单文件 50KB **严重违反项目 800 行上限，移植时必须拆**。

**结果卡片元信息行抄 RAGFlow**：`testing-result.tsx` 顶部把三个分数以斜体小字并排：

```
{ field: 'similarity',        label: 'Hybrid Similarity' },
{ field: 'term_similarity',   label: 'Term Similarity' },
{ field: 'vector_similarity', label: 'Vector Similarity' },
```

配套 `web/src/components/similarity-slider/index.tsx` 是 `vector_similarity_weight` 调参滑块。

> ⚠️ RAGFlow 的高亮是**关键词回填**，与 kbweb 的偏移量契约冲突，这块别抄。

### 4.2 其他调参与可视化参考

| 项目 | star | license | 最后 push | 价值 |
|---|---|---|---|---|
| [`qdrant/qdrant-web-ui`](https://github.com/qdrant/qdrant-web-ui) | 419 | Apache-2.0 | 2026-08-04 | 后端就是 Qdrant，栈天然对齐。`SearchQualityPanel.jsx` + `check-index-precision.js` 量化 HNSW 近似检索的精度损失；`VisualizeChart/` 用 PCA + WebGL 散点摊开命中集合。**纯 dense 视角，无 sparse 路** |
| [`o19s/quepid`](https://github.com/o19s/quepid) | 343 | Apache-2.0 | 2026-08-07 | 相关性评测工业标准。抄**概念模型**（case / query / rating / snapshot / diff）而非代码——把调参从肉眼观察变成可回归流程。Rails + AngularJS 老栈 |
| [`langfuse/langfuse`](https://github.com/langfuse/langfuse) | 32691 | ⚠️ MIT + `ee/` 企业许可 | 2026-08-07 | trace 详情页的「时间轴 + 可折叠嵌套 span」布局。抄前须确认目标文件不在 `web/src/ee/` |
| [`Arize-ai/phoenix`](https://github.com/Arize-ai/phoenix) | 10929 | ⚠️ **Elastic License 2.0（非 OSI）** | 2026-08-07 | retrieval evaluation 视图值得看，**只看不抄** |
| [`explodinggradients/ragas`](https://github.com/explodinggradients/ragas) | 15173 | Apache-2.0 | 2026-02-24 | 纯评测库，**基本没有 UI**（pandas/notebook 输出）。对界面诉求帮助有限 |

---

## 5. 搜索 UI 组件库：抄契约，不抄组件

这一类全是受控 React/Vue 组件，与 Flask SSR 正面冲突。价值在**设计契约**。

| 项目 | star | license | 最后 push | 值得抄什么 |
|---|---|---|---|---|
| [`algolia/instantsearch`](https://github.com/algolia/instantsearch) | 4055 | MIT | 2026-08-07 | `Highlight` / `Snippet` widget 的契约：组件只接收后端已标注的片段，前端零匹配逻辑——**与 kbweb「后端给偏移、前端只切分」同一哲学**。`CurrentRefinements` + `ClearRefinements` 是多维 facet 的可撤销 chip 标准模式 |
| [`elastic/search-ui`](https://github.com/elastic/search-ui) | 1979 | Apache-2.0 | 2026-08-05 | **URL 状态同步**（`trackUrlState`）：检索词、模式、过滤器、分页全落 query string。kbweb 的「模式切换 + 来源/章节过滤」正需要，且是可分享链接的前提 |
| [`searchkit/searchkit`](https://github.com/searchkit/searchkit) | 4859 | Apache-2.0 | 2026-04-04 | adapter 层范本：如何把自家 REST 响应映射成 InstantSearch 数据结构 |
| [`typesense/typesense-instantsearch-adapter`](https://github.com/typesense/typesense-instantsearch-adapter) | 525 | MIT | 2026-07-08 | 约 200 行的**最小 adapter 实现**，演示 highlight 字段 / facet counts / 分页参数三者对齐。想写 shim 抄这个比抄 Searchkit 快 |
| [`meilisearch/meilisearch-js-plugins`](https://github.com/meilisearch/meilisearch-js-plugins) | 532 | MIT | 2026-08-01 | Meilisearch 本身做 hybrid search，可看 `semanticRatio` 之类混合参数如何暴露给 UI |

---

## 6. RAG 系统自带前端

| 项目 | star | license | 最后 push | 结论 |
|---|---|---|---|---|
| [`infiniflow/ragflow`](https://github.com/infiniflow/ragflow) | 87022 | **Apache-2.0** | 2026-08-07 | 本类最值得看且 license 最干净。见 §4 |
| [`Mintplex-Labs/anything-llm`](https://github.com/Mintplex-Labs/anything-llm) | 64456 | **MIT** | 2026-08-06 | 引用折叠卡交互（默认收起、展开显示原文片段 + 文档名 + 相似度）可直接用。但无独立检索页，一切在 chat 语境 |
| [`onyx-dot-app/onyx`](https://github.com/onyx-dot-app/onyx) | 31480 | ⚠️ MIT + Onyx Enterprise | 2026-08-07 | **license 恰好卡在要看的地方**：`web/src/ee/sections/SearchUI.tsx`、`SearchCard.tsx`、`ee/lib/search/svc.ts` 全在 ee 下，**不可借鉴**。MIT 侧可看 `components/search/DocumentDisplay.tsx`、`components/filters/SourceSelector.tsx` |
| [`langgenius/dify`](https://github.com/langgenius/dify) | 151690 | ⚠️ 改版 Apache-2.0 | 2026-08-07 | LICENSE 明确禁止移除或修改**前端 LOGO 与版权信息**，且「前端」定义为 `web/` 目录全部内容。**`web/` 下任何东西都不建议移植** |
| [`khoj-ai/khoj`](https://github.com/khoj-ai/khoj) | 36369 | ⚠️ **AGPL-3.0** | 2026-08-02 | 传染性，只看不抄 |
| [`xr843/fojin`](https://github.com/xr843/fojin) | 327 | Apache-2.0 | 2026-08-07 | **和 kbweb 最像的同类系统**：佛教数字文本平台，10500+ 文本、613 来源、三语跨藏检索 + AI 问答。看它的检索结果呈现与跨藏对照的信息架构 |
| [`linagora/openrag`](https://github.com/linagora/openrag) | 237 | ⚠️ AGPL-3.0 | 2026-08-02 | `ui/src/pages/admin/presets.tsx` 有 RRF 检索预设配置界面。AGPL + 社区薄，仅视觉参考 |
| [`morphik-org/morphik-core`](https://github.com/morphik-org/morphik-core) | 3704 | ⚠️ **BSL 1.1** | 2026-07-23 | 非 OSI 开源（Change Date 2029-06-18）。排除 |

**已排除**：`weaviate/Verba`（7713★ 但**已 archived**）、`QuivrHQ/quivr`（39392★，push 2025-07-09，
已转型 SDK、前端弃管）、`SciPhi-AI/R2R-Application`（176★，push 2025-05-02，15 个月停更）。

---

## 7. 阅读器与锚定

### 7.1 一条被下调的建议

第三路调研提出「偏移量锚点很脆，切块变了就全断」，建议引入 Hypothesis 的多重 selector 降级链。
**白盒验证后这条对检索结果高亮不成立。**

`kbweb/filters.py:33-39` 的 `highlight_segments` 已经做了防御：

```python
if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
```

span 越界或重叠一律丢弃，坏负载**退化成纯文本**而不是打乱正文。而且 `text` 与 `spans`
来自同一次响应，天然自洽，不存在漂移。

真正有漂移风险的只是 `/read/<id>` 的深链锚定——重建索引后 chunk id 变了，
分享出去的链接会失准。这是个小得多的问题。

| 项目 | star | license | 最后 push | 价值 |
|---|---|---|---|---|
| [`hypothesis/client`](https://github.com/hypothesis/client) | 717 | BSD-2-Clause | 2026-08-05 | `src/annotator/anchoring/` 的多重 selector 降级链：`TextPositionSelector`（偏移，快但脆）→ `TextQuoteSelector`（引文 + 前后缀，慢但抗漂移）→ `RangeSelector`。**对深链锚定有价值，对结果高亮无必要** |

### 7.2 阅读器技术底座

| 项目 | star | license | 最后 push | 价值 |
|---|---|---|---|---|
| [`johnfactotum/foliate-js`](https://github.com/johnfactotum/foliate-js) | 1056 | **MIT** | 2026-05-01 | **最干净的底座**：无框架无依赖。核心价值是**定位模型**——CFI 与 `getTarget()` / `select()` API，「给我一个位置标识，滚到那儿并标出来」正是 kbweb「锚定到命中段」需要的抽象。把 CFI 换成「文档 id + 段 id + 字符偏移」即可 |
| [`futurepress/epub.js`](https://github.com/futurepress/epub.js) | 6941 | BSD-3 类 | 2026-03-24 | `Rendition` / `Location` / `Annotation` 三层分离。**关键**：高亮用绝对定位 SVG rect 叠加层而非 wrap `<mark>`，不破坏原文 DOM 与偏移量的对应关系——正好契合 kbweb 的契约 |
| [`GoogleChromeLabs/text-fragments-polyfill`](https://github.com/GoogleChromeLabs/text-fragments-polyfill) | 128 | Apache-2.0 | 2026-06-22 | URL Text Fragments（`#:~:text=`）。「可分享的深链直达某段」的浏览器原生语义参考，可作降级兜底 |
| [`readest/readest`](https://github.com/readest/readest) | 23179 | ⚠️ **AGPL-3.0** | 2026-08-07 | 连续滚动 + 分页双模式、批注锚定、进度记忆做得完整。**只作交互参考，别拷代码**（其内部渲染用的正是 foliate-js） |
| [`ambuda-org/ambuda`](https://github.com/ambuda-org/ambuda) | 120 | MIT | 2026-05-11 | 梵语数字图书馆。**做得最好的「古典语言阅读界面」参考**：长篇古典文本单栏阅读、逐词注释联动、扫描页与转写对照。看布局不看色彩 |

**已排除**：`agentcooper/react-pdf-highlighter`（1402★，push 2024-11-22，超 18 个月红线）及其
分支 `DanielArnould/react-pdf-highlighter-extended`（同日停更）。
`zotero/reader`（202★）license 文件为 `COPYING`，GitHub 标 Other，未定性，不推荐作移植源。

---

## 8. CSS 基础层：只做两件小事

| 项目 | star | license | 最后 push | 建议 |
|---|---|---|---|---|
| [`sindresorhus/modern-normalize`](https://github.com/sindresorhus/modern-normalize) | 7379 | MIT | 2026-06-25 | **成本最低、收益最确定的一条**：若 `app.css` 顶部仍手写 reset，直接 vendor 这一个文件替换 |
| [`argyleink/open-props`](https://github.com/argyleink/open-props) | 5490 | MIT | 2026-01-31 | 有现成的 `src/props.colors-oklch.css` 与 `props.gray-oklch.css`，正对 kbweb 的 oklch 取向。**抄色阶命名法与 easing/shadow 组，不整包引**（最后 release 是 2023-09 的 v1.6.0，节奏很慢） |

**不要整包引入任何 classless 框架。** 它们的价值恰恰在于「你还没有设计取向」，
kbweb 已有明确立场，引进来只会打架。

| 项目 | star | license | 最后 push | 为何不引 |
|---|---|---|---|---|
| [`picocss/pico`](https://github.com/picocss/pico) | 16766 | MIT | 2026-05-09 | 变量三层分法（原始值 → 语义变量 → 组件变量）值得看，表单元素样式可对照。但**色彩体系是 HSL，无 oklch**，与 kbweb 方向不一致 |
| [`bigskysoftware/missing`](https://github.com/bigskysoftware/missing) | 797 | BSD-2 | 2026-06-13 | 哲学最贴近（承认纯 classless 不够，给克制的类名扩展）。但生态小，风格偏工程感 |
| [`kevquirk/simple.css`](https://github.com/kevquirk/simple.css) | 4967 | MIT | 2026-07-19 | 阅读器排版基线可对照。classless 无法表达结果卡片、任务状态等结构组件 |
| [`dbohdan/classless-css`](https://github.com/dbohdan/classless-css) | 2368 | 无 license | 2026-04-03 | 69 个主题的带截图目录，**纯查找工具**，别当依赖 |

**已停更**：`kognise/water.css`（8648★，2024-02）、`xz/new.css`（4038★，2024-03）、
`vladocar/Basic.css`（688★，2021-01）、`elad2412/the-new-css-reset`（2342★，2024-08）。
**因构建链排除**：`hunvreus/basecoat`（4223★ / MIT / 活跃，但基于 Tailwind）。

---

## 9. htmx / Jinja 组件化：要么两个一起上，要么都不上

### 9.1 Flask + htmx 没有值得推荐的模板

| 项目 | 排除原因 |
|---|---|
| `edmondchuc/flask-htmx` | 156★，push 2024-09-22（23 个月）。且只是把 `HX-Request` 头包一层，20 行的事 |
| `testdrivenio/flask-htmx-tailwind` | 87★，push 2024-01-31 |
| 其余 flask-htmx 搜索结果 | 全部 ≤37★，多为教程 / hello-world |

### 9.2 库本体才是实际会用的

| 项目 | star | license | 最后 push | 说明 |
|---|---|---|---|---|
| [`bigskysoftware/htmx`](https://github.com/bigskysoftware/htmx) | 48901 | BSD-2（GitHub 标 NOASSERTION） | 2026-08-05 | 单文件约 14KB，`static/vendor/htmx.min.js` 一放即用，零构建零 npm。**kbweb 唯一真正符合约束的「SPA 级交互」路径**，且渐进增强模型天生服务「JS 挂了仍能用」 |
| [`alpinejs/alpine`](https://github.com/alpinejs/alpine) | 31845 | MIT | 2026-07-28 | 同样单文件可 vendor。但对 6 个页面很可能过度工程 |
| [`starfederation/datastar`](https://github.com/starfederation/datastar) | 4815 | MIT | 2026-08-05 | htmx + Alpine 的 SSE 驱动替代品。对任务轮询页有意义，但生态远不如 htmx 成熟。**可以看，别现在押注** |

**关键判断**：若引 htmx，就应连带引 [`sponsfreixes/jinja2-fragments`](https://github.com/sponsfreixes/jinja2-fragments)
（366★ / MIT / 2026-04-16），否则分页与任务轮询会被迫为每个片段单开模板文件。
这是**要么两个一起上、要么都不上**的决定。

而 kbweb 目前 4 个原生 JS 文件（`theme.js` / `drawer.js` / `reader.js` / `jobs.js`）
还没到撑不住的规模。

> 💡 `hunvreus/devpush`（4739★ / MIT / 2026-03-03）虽因依赖 npm 构建链而不适配，
> 但它的 `cp node_modules/.../dist/*.min.js ./assets/` 脚本揭示了一个可用做法：
> **htmx / Alpine 的分发产物就是单文件，vendored 进 `static/` 即可，完全不需要 npm。**

### 9.3 Jinja 组件化：现在什么都不做

| 项目 | star | license | 最后 push | 判断 |
|---|---|---|---|---|
| [`jpsca/jinjax`](https://github.com/jpsca/jinjax) | 430 | MIT | 2026-06-18 | 唯一够格的候选（`<Card title="x">` 组件语法、props + slot、零构建纯 Python 侧）。**但作者已在推后继项目 `jx`（有 `jpsca/jx-migrate` 迁移工具），是真实押注风险**；且引入非标准 Jinja 语法会退化编辑器高亮与 `jinja-lsp` |
| [`mikeckennedy/jinja_partials`](https://github.com/mikeckennedy/jinja_partials) | 237 | MIT | 2026-07-22 | 几十行代码，解决 `{% include %}` 依赖外层变量名的耦合。**真到重复难忍时，看一眼实现自己写个 Jinja global 即可，比引依赖划算** |
| [`volfpeter/htmy`](https://github.com/volfpeter/htmy) | 399 | MIT | 2026-07-22 | **跳过**。它是 Jinja 的替代品而非补强，采用意味着放弃全部现有 `templates/` |

6 个页面的规模大概率还没到 `{% macro %}` 撑不住的临界点。

---

## 10. 明确跳过

| 方向 | 理由 |
|---|---|
| **TEI Publisher 全链** | eXist-db + XQuery 后端，且 `eeditiones/tei-publisher-components` 是 **GPL-3.0 传染**（主应用 `tei-publisher-app` 更无 license 声明）。若日后语料有 TEI 标注，走 [`TEIC/CETEIcean`](https://github.com/TEIC/CETEIcean)（183★ / **BSD-2** / 2026-06-25）——只把 XML 变成 custom elements，样式仍归你写，与手写 CSS 取向兼容 |
| **Omeka S / Islandora** | PHP / Drupal，主题层无任何可迁移 CSS。Islandora 主题生态实质已死（`islandora-deprecated/carapace` 明标 DEPRECATED，其余停在 2015-2016） |
| **Vivliostyle** | 776★ / **AGPL-3.0** / 活跃。CJK 竖排 + 分页最完整，但解决的是**分页印刷**，kbweb 是连续滚动。除非将来做「导出古籍版式 PDF」 |
| **LaTeX.css / Gutenberg** | `vincentdoerig/latex-css`（3472★）气质是学术论文不是善本；`matejlatin/Gutenberg`（2845★，2022-11 停更）纯拉丁排版。都是「另一种 opinionated 外观」，混进来只会稀释已有立场 |
| **IIIF 全家桶** | 除非接书影扫描页，完全用不上。真要接时选 [`samvera-labs/clover-iiif`](https://github.com/samvera-labs/clover-iiif)（86★ / MIT / 2026-07-07，React 组件、易主题化），**别选 Mirador**（613★ / Apache-2.0，Material-UI 会整块进来，与「灯下绢本」正面冲突）。注意 samvera-labs 是实验室命名空间，同组织的 `nectar-iiif`、`bloom-iiif` 已停更。`digirati-co-uk/canvas-panel`（34★，2023-11）事实停更 |
| **Astro / Hugo 通用主题生态** | `--topic=astro-theme` 前 20 全是 AstroWind / AstroPaper / Astroship 这类 Tailwind 落地页模板，正是要避开的。唯一例外见下 |
| **`garywill/vert-cjk-web`** | 78★ / AGPL-3.0 / 2022-10 停更、作者自标 alpha。竖排走 heti 的 `writing-mode.scss` 或 Han.css |
| **`zmmbreeze/Entry.css` / `lining.js`** | 2015 / 2020 停更，无 license。思路已被 `text-spacing-trim` 等原生特性取代，仅作历史阅读 |

**唯一例外的参数来源**：[`radishzzz/astro-theme-retypeset`](https://github.com/radishzzz/astro-theme-retypeset)
（684★ / MIT / 2026-04-12）「重新编排」，明确以纸质书阅读体验为目标，六语支持、中文一等公民。
**单独看它的 CSS 变量表**——中文正文测量宽度、段间距、行高的取值组合，以及中英混排字体切换策略。
当第二意见校准 `--leading-read` / `--measure-read`，**不当代码来源**（Astro + UnoCSS 架构无交集）。
同类的 `moeyua/astro-theme-typography`（618★，2026-02-17）是其灵感来源，取其一即可。

### 10.1 视觉参考（非代码）

| 项目 | star | license | 最后 push | 用途 |
|---|---|---|---|---|
| [`shanleiguang/vRain`](https://github.com/shanleiguang/vRain) | 1644 | MIT | 2026-06-14 | 中文古籍**刻本风格直排**电子书生成器（Perl → PDF）。价值在其背景图配置：栏线、鱼尾、版心、天头地脚的比例关系，以及纸张墨色取值。想让 `--paper-edge` / `--shadow-page` 更像善本时的最佳比例来源。**技术栈零重叠，别试图移植** |
| [`edwardtufte/tufte-css`](https://github.com/edwardtufte/tufte-css) | 6539 | MIT | 2026-06-24 | 只有一样值得抄：**边注（sidenote）的纯 CSS 实现**——`<label>` + 隐藏 checkbox + `float`，桌面端浮在正文右侧，窄屏塌陷为可展开内联注。**无 JS**，是「左侧 ordinal 刻度 + 右侧注疏」的自然延伸。⚠️ 字体 / 配色 / 1400px 双栏总宽都是拉丁语境，**CJK 下它的 1.5rem 行高完全不够**，kbweb 的 1.9/1.95 是对的 |
| [`programminghistorian/jekyll`](https://github.com/programminghistorian/jekyll) | 548 | 无 license 声明 | 2026-08-07 | 真实运行多年的多语言学术长文站，正文版式与目录导航经实战检验。**看渲染结果，不看 Jekyll 代码** |

---

## 11. License 红线汇总

自托管开源项目需回避或谨慎：

| 授权 | 项目 | 影响 |
|---|---|---|
| **AGPL-3.0** | `khoj-ai/khoj`、`readest/readest`、`linagora/openrag`、`vivliostyle/vivliostyle.js`、`simple-login/app`、`garywill/vert-cjk-web` | 传染性，只看不抄 |
| **MIT + `ee/` 企业许可** | `onyx-dot-app/onyx`（搜索 UI 恰好全在 `web/src/ee/`）、`langfuse/langfuse` | 抄前须确认目标文件不在 ee 下 |
| **改版 Apache-2.0** | `langgenius/dify` | LICENSE 明确禁止修改 `web/` 目录的 LOGO 与版权信息，条款直接针对前端 |
| **BSL 1.1** | `morphik-org/morphik-core` | 非 OSI 开源，Change Date 2029-06-18 |
| **Elastic License 2.0** | `Arize-ai/phoenix` | 非 OSI 开源，禁止作为托管服务提供给第三方 |
| **GPL-3.0** | `eeditiones/tei-publisher-components`、`omeka/omeka-s` | 传染性 |
| **无 license / NOASSERTION** | `sofish/typo.css`、`dbohdan/classless-css`、`programminghistorian/jekyll`、`tonyhuan/GuanKiapTsingKhai`、`zotero/reader`、`hoveychen/guwen-reader`、`lhw828/GuWen` | 无授权声明 = 默认保留所有权利，不可拷贝 |

---

## 12. 两个诚实的判断

**一、中文古籍前端生态基本是空的。**
ctext.org / 殆知阁 / 中国哲学书电子化计划**都没有开源前端**——`ctext` 关键词命中的全是同名无关项目，
`dsturgeon` 名下无公开仓库。**Kanripo 生态已死**：`kanripo/krp-docs`（1★）、
`DHSinology/Kanripo-data`（4★ / 2024）、`kanripox/kanripox-dev`（0★ / 2022）全是数据仓库，无前端。
`hoveychen/guwen-reader`（1★，无 license）与 `lhw828/GuWen`（45★，无 license，2025-02 停更）
只有语料价值。**这一类里能真正用的只有 heti 一个，这就是全部。**

**二、检索调试抽屉在 GitHub 上确实没有先例。** 见 §4 的搜索证据。
kbweb 在这一点上不是自我感觉良好——它是真的空白。

---

## 13. 行动清单

按投入产出排序。

### 必做 — 已于 2026-08-08 全部落地

1. ✅ **字体自托管 + 子集化**（§2）——`deploy/build-fonts.py`：抓取 Noto Serif SC 与霞鹜文楷
   （均 OFL-1.1），经 `cn-font-split` 切成 382 + 313 个 unicode-range 分片。
   `tokens.css` 拆出 `--font-song` / `--font-kai`，`--font-serif` 保留为别名。
   脚本没跑过时 `base.html` 不 link 字体样式表，回落本机字体栈
2. ✅ **宋楷分工**（§3）——楷体用于 `.reader__title` 与 `.chunk__heading`；
   heti 的行高下限（着重号 1.7 / ruby 2.0）作为约束写进 `tokens.css` 注释。
   未引 `heti-addon.js`
3. ✅ **把 no-JS 做成 pytest 维度**（§1）——`KBWEB_NOJS` 配置项 + `conftest.config`
   参数化，测试数 44 → 87。**这个维度当场抓到一个真 bug**：调试抽屉带 `hidden`
   属性且只有 `drawer.js` 能揭开，无脚本时轨迹在 DOM 里却永不显示；现改为内联渲染
4. ⚠️ **标点禁则**（§3.2）——见该节更正。实际只加了 `line-break: strict`，
   因为初稿判断有误
5. ✅ **标点挤压**（§3.1）——`text-spacing-trim` + `text-autospace`，
   均以 `@supports` 包裹，零运行时成本

> **刻意未做**：古文段首缩进 2em。`.chunk__text` 用 `white-space: pre-wrap`，
> 块级 `text-indent` 只作用于首行，多段落切块会得到不一致的缩进——这不是移植 heti
> 规则，是引入排版 bug。等切块粒度确定后再单独处理。

### 可做（工作量最大，但独特价值最高）

6. **调试抽屉三合一移植**（§4）——布局与连线抄 OpenSearch `visual_comparison/`，
   数据契约抄 HMS 的 `trace: true`，分数元信息行抄 RAGFlow。三者 license 均可安全移植

### 有明确场景才做

7. **边注**：抄 tufte-css 的 sidenote 纯 CSS 机制，配色字体全用自己的（§10.1）
8. **URL 状态同步**：参考 elastic/search-ui 的 `trackUrlState`（§5）
9. **深链锚定加兜底**：参考 hypothesis/client 的 quote + prefix/suffix selector（§7.1）
10. **参数校准**：对照 retypeset 的 CSS 变量表复核 `--leading-read` / `--measure-read`（§10）
11. **CSS 基础层**：vendor `modern-normalize`；从 open-props 抄 oklch 色阶命名法（§8）

---

*调研方法：`gh search repos` / `gh search code` / `gh api repos/...` 三路并行，
按「Flask SSR 脚手架」「搜索 RAG 前端」「古籍 editorial 界面」分工。
所有 star / license / push 数据为 2026-08-07 实测。*

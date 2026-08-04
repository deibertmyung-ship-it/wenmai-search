# kbweb 代码架构分析

> 本文的结论来自代码知识图谱的静态分析（401 节点 / 1546 边），不是对设计意图的复述。
> 复杂度、依赖方向、环检测均为实测值。
> 索引名 `kbweb`，索引时间 2026-08-02。

## 1. 规模

| 维度 | 数值 |
|---|---|
| 生产代码 | Python 23 文件 · HTML 13 · JavaScript 4 · CSS 2，合计 2260 行 |
| 测试 | 37 单元 + 50 E2E |
| 图节点 | 401（Function 136 / Method 42 / Class 19 / Route 11 / EnvVar 9） |
| 图边 | 1546（DEFINES 557 / USAGE 351 / CALLS 193 / SEMANTICALLY_RELATED 126） |
| Flask 路由 | 11 条 |

最大的几个文件：

| 文件 | 行数 | 说明 |
|---|---|---|
| `static/css/app.css` | 511 | 组件样式。全项目最大的单文件 |
| `client.py` | 188 | kbsvc REST 的唯一出口 |
| `static/css/tokens.css` | 135 | 设计令牌 |
| `filters.py` | 117 | Jinja 过滤器 |
| `templates/search.html` | 112 | 检索页 |
| `static/js/reader.js` | 112 | 阅读器渐进增强 |

**CSS 占了全项目 29% 的行数**（646 / 2260）。对一个服务端渲染、无构建步骤的前端来说这是正常比例——样式没有被打包工具切碎，全部是可读的手写 CSS。

## 2. 包依赖：三层 DAG

对 `kbweb/` 下所有相对导入做 AST 提取后做环检测，**结果是 NONE**。

```
   ┌──────────────────────────────────────┐
   │ (root)  __init__ / filters / config  │   应用装配层  out=5
   │         errors / client              │
   └───────────────┬──────────────────────┘
                   │
                   ▼
   ┌──────────────────────────────────────┐
   │ views   search / library / ingest    │   视图层  out=3, in=1
   │         jobs / api / _common         │
   └──────┬──────────┬──────────┬─────────┘
          ▼          ▼          ▼
     ┌────────┐ ┌────────┐ ┌────────┐
     │ client │ │ config │ │ errors │      基础层  out=0
     │  in=2  │ │  in=2  │ │  in=2  │
     └────────┘ └────────┘ └────────┘
```

| 包 | out | in | 依赖 |
|---|---|---|---|
| `(root)` | 5 | 0 | client, config, errors, views |
| `views` | 3 | 1 | client, config, errors |
| `client` | 0 | 2 | — |
| `config` | 0 | 2 | — |
| `errors` | 0 | 2 | — |

依赖图只有三层、五个节点。这不是"架构简单"，而是**前端刻意不承担任何领域逻辑**的结果：没有 model 层、没有 service 层、没有状态管理层，因为检索逻辑全在 kbsvc 里。视图只做「解析请求 → 调 client → 渲染」。

## 3. 唯一的后端出口

图分析给出的扇入排名：

| 符号 | 扇入 | 说明 |
|---|---|---|
| `dict.get` | 43 | 内置 |
| `KbClient._request` | **14** | 所有后端调用的收口 |
| `views._common.client` | 13 | 获取请求作用域的 client |
| `filters.highlight_segments` | 6 | 高亮切分 |
| `views._common.as_int` | 4 | 参数钳制 |
| `create_app` | 3 | 应用工厂 |

**`KbClient._request` 扇入 14 且没有旁路**——`client.py` 的 19 个公开方法全部经它转发，没有任何视图直接构造 httpx 请求。这一条收口带来三个可验证的性质：

1. **认证只在一处**：`Authorization: Bearer` 头在 `KbClient.__init__` 设置，API Key 不可能泄漏到浏览器（有测试 `test_api_key_is_sent_to_the_backend_but_never_to_the_browser` 守着）
2. **超时只在一处**：`httpx.Client(timeout=...)`
3. **错误翻译只在一处**：`_parse_error` 把后端信封转成 `BackendError`，`httpx.HTTPError` 转成 `BackendUnavailable`

`views` 包 out=3、只依赖 client/config/errors，从依赖方向上就排除了"某个视图偷偷绕过 client 直连后端"的可能。

## 4. 复杂度：全项目最高认知复杂度只有 8

| 函数 | 文件 | 圈复杂度 | 认知 | 循环深度 | 行数 |
|---|---|---|---|---|---|
| `highlight_segments` | filters.py | 6 | 8 | 1 | 28 |
| `search.index` | views/search.py | 5 | 8 | 0 | 53 |
| `timeago` | filters.py | 6 | 8 | 1 | 15 |
| `_parse_error` | client.py | 4 | 7 | 0 | 16 |
| `_request` | client.py | 4 | 5 | 0 | 12 |
| `ingest.upload` | views/ingest.py | 4 | 5 | 0 | 24 |
| `reader.js:load` | static/js/reader.js | 3 | 3 | 0 | 39 |

对比后端最高 22，前端最高 8。**没有一个函数需要重构**。

三个 8 分函数各自的复杂度来源不同，都不是写法问题：

- **`highlight_segments`**：要处理 span 越界、重叠、空文本三类退化输入。这些分支是刻意的——后端给的偏移一旦不可信，必须降级成纯文本而不是打乱原文。有 6 个测试专门覆盖这些分支。
- **`search.index`**：11 个查询参数要解析、钳制、回填表单。其中隐藏的 `f=1` 标记位逻辑（区分"复选框未勾选"与"表单未提交"）本身就值 2 分认知复杂度，但去掉它会产生一个真实 bug。
- **`timeago`**：多档相对时间 + 非法输入退化。

## 5. 渐进增强：图上可见的降级路径

前端声称「JS 全挂时页面仍可用」。这在结构上是可验证的——4 个 JS 文件的调用图与 Python 视图**没有任何交叉**：

| 脚本 | 行数 | 增强 | 降级路径 |
|---|---|---|---|
| `reader.js` | 112 | 原地追加下一批 chunk | 服务端渲染的 `<a href="?from=N">` 链接 |
| `drawer.js` | 60 | 调试抽屉开合 | 抽屉内容仍在 DOM 中，可滚动 |
| `jobs.js` | 40 | 任务轮询 + 退避 | 手动刷新 |
| `theme.js` | 25 | 主题记忆 | 跟随 `prefers-color-scheme` |

JS 只通过 `/api/*` 两个端点取数（`api.chunks`、`api.jobs`），这两个端点在图上是独立的 Route 节点，不参与任何服务端渲染路径。换言之：**删掉整个 `static/js/` 目录，11 条路由里没有一条会失效**。

`reader.js:load` 用 `textContent` 写入正文（从不用 `innerHTML`），这是 XSS 面上的硬约束，在 E2E 里由「无 console error」断言间接覆盖。

## 6. 社区检测

| 簇 | 内聚度 | 代表成员 | 说明 |
|---|---|---|---|
| 15 | **0.88** | `create_app`, `live_server`, `_flag`, `Config` | 应用装配 + 配置。满内聚 |
| 8 | 0.85 | `highlight_segments`, 分页测试 | 高亮与分页，与测试强绑定 |
| 24 | **1.00** | `short_id`, `ms`, `score`, `filesize` | 格式化过滤器。满内聚，纯函数 |
| 80 | 0.57 | `client`, `read`, `index`, `chunks`, `as_int` | 视图层，横跨多个 blueprint |
| 30 | 0.55 | `_request`, `shelf`, `list_sources`, `list_documents` | client 与其调用方 |

簇 24 内聚度 1.0 且全是纯函数（无 I/O、无状态）——`filters.py` 的格式化部分可以整体提取复用，也解释了它为何能被 100% 单元测试覆盖。

## 7. 配置面

图抓到 9 个 `EnvVar` 节点与 `CONFIGURES` 边，全部集中在 `config.py`：

| 变量 | 用途 | 安全相关 |
|---|---|---|
| `KBWEB_API_BASE` | kbsvc 地址 | |
| `KBWEB_API_KEY` | 转发凭据 | **不下发浏览器** |
| `KBWEB_SECRET_KEY` | Flask session | 生产必须固定 |
| `KBWEB_DEBUG_UI` | 是否允许请求 debug 负载 | 生产可关 |
| `KBWEB_TIMEOUT` / `PAGE_SIZE` / `READER_PAGE_SIZE` / `MAX_UPLOAD_BYTES` / `TEMPLATE_RELOAD` | 行为调节 | |

`Config` 是 `@dataclass`，每个字段用 `field(default_factory=...)` 读环境变量——**没有模块级的环境变量读取**，因此测试可以直接构造 `Config(...)` 覆盖，不需要 monkeypatch 环境。37 个单元测试全部走这条路。

## 8. 前后端边界

跨仓库匹配（`cross-repo-intelligence`）返回 **0 条边**。原因是 kbweb 的调用形如 `self._request("POST", "/v1/search")`——路径是方法体内的字符串字面量，静态分析无法把它与 kbsvc 的 Route 节点关联。

这不影响可维护性：**边界就是 `client.py` 一个文件，19 个方法一一对应后端路由**，读它比读图更直接。对照表：

| KbClient 方法 | kbsvc 路由 |
|---|---|
| `search` | `POST /v1/search` |
| `list_sources` / `create_source` | `GET|POST /v1/sources` |
| `list_documents` / `get_document` | `GET /v1/documents[/{id}]` |
| `get_chunks` | `GET /v1/documents/{id}/chunks` |
| `reindex_document` | `POST /v1/documents/{id}/reindex` |
| `delete_document` | `DELETE /v1/documents/{id}` |
| `upload` / `ingest_path` | `POST /v1/ingest/upload` · `/path` |
| `list_jobs` / `retry_job` | `GET /v1/jobs` · `POST /v1/jobs/{id}/retry` |
| `stats` / `health` | `GET /v1/stats` · `/healthz` |

**维护提示**：后端路由变更时，唯一需要同步的文件就是 `client.py`。若要让这条边界机器可检，可以在 CI 里拉 kbsvc 的 OpenAPI schema 与 `client.py` 的方法表比对——目前没做。

## 9. 结论与建议

**结构上没有需要处理的问题。** 依赖三层无环、后端调用单点收口、最高认知复杂度 8、JS 与服务端渲染路径零交叉。

| 优先级 | 事项 | 依据 |
|---|---|---|
| 低 | `app.css` 511 行，若继续增长可按组件拆分 | 目前仍在可读范围，拆分会引入 @import 或构建步骤 |
| 低 | CI 中比对 kbsvc OpenAPI 与 `client.py` 方法表 | 跨仓库边界目前靠人工同步 |

**不建议动的地方：** `views` 不加 service 层（领域逻辑在后端，加了就是空转发）；`highlight_segments` 的分支（每一条都对应一类退化输入，有测试守着）；`search.index` 的 `f=1` 标记位（去掉会产生真实 bug）。

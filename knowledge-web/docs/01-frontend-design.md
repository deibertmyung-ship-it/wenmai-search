# kbweb 前端方案（Flask）

## 1. 定位与边界

kbweb 是 kbsvc 的**表现层**，不是第二个后端。它：

- 通过 HTTP 调用 kbsvc REST API，**不直接连数据库、不直接连 Qdrant 或 Tantivy**
- 不持有任何检索逻辑（rewrite/fusion/rerank 全在 kbsvc）
- 只负责：会话、表单、渲染、渐进增强

**为什么必须走 HTTP 而不是直接 import kbsvc**：当前 `local` profile 将嵌入式 Qdrant、
Tantivy 词法索引与常驻 worker 都放在 API 进程中，两者都持目录锁，Web 不应成为第二个
持有者。HTTP 边界让 Web 不复制检索、队列和存储逻辑，也能直接指向远程 kbsvc，使两种
profile 行为一致。

```
浏览器 ──▶ Flask (kbweb) ──HTTP──▶ kbsvc REST ──▶ Qdrant / Tantivy / PG / S3
             │
             └─ Jinja SSR + 少量原生 JS（无构建步骤、无 CDN）
```

## 2. 为什么是服务端渲染

- 检索结果是**文本**，首屏内容即价值，SSR 首字节就能读
- 语料是古籍，用户会长时间阅读——SPA 的路由与状态开销在这里没有收益
- 无构建链（webpack/vite）意味着部署就是 `flask run`

交互性靠三处渐进增强，全部原生 JS，无框架：检索调试抽屉、阅读器的上下文续读、任务轮询。
JS 全部失效时，页面仍可搜索、可翻页、可上传——这是硬性要求。

## 3. 视觉方向：古籍善本 / Editorial

不做通用后台模板。具体承诺：

| 维度 | 决策 |
|---|---|
| 底色 | 暖纸白 `oklch(97.5% 0.012 85)`，非纯白；深色主题为墨色 `oklch(16% 0.008 60)` |
| 正文字体 | CJK 衬线栈（Songti SC / SimSun / Noto Serif CJK SC）——正文是文言文，衬线是语义正确的选择，不是装饰 |
| 界面字体 | 系统无衬线，与正文形成**字体对比**而非字号对比 |
| 强调色 | 朱砂红 `oklch(48% 0.17 25)`，**只用于引用标记与命中高亮**（语义色，不是装饰色） |
| 次强调 | 靛青 `oklch(42% 0.09 250)`，只用于调试与度量数据 |
| 层次 | 排名序号用超大号衬线数字（scale contrast），与正文形成 6:1 的字号比 |
| 节奏 | 结果之间用不等距分隔——首条留白更大，形成阅读入口 |
| 深度 | 检索栏 sticky + 纸张边缘阴影；调试抽屉从右侧覆盖，非内联展开 |

主题：**浅色为默认**（纸张隐喻），深色可切换，两套都要看起来是有意设计的。

## 4. 页面与路由

| 路由 | 页面 | 核心任务 |
|---|---|---|
| `GET /` | 检索 | 查询、模式切换、过滤、结果列表、调试抽屉 |
| `GET /read/<document_id>` | 阅读器 | 按 ordinal 连续读，锚定到某个 chunk |
| `GET /library` | 书库 | 按 source 分组的文档列表、搜索、统计 |
| `GET /library/<document_id>` | 文档详情 | 版本历史、chunk 数、解析器、原始 URI |
| `GET /ingest` | 导入 | 上传表单、按路径批量导入、source 管理 |
| `GET /jobs` | 任务 | 状态分组、失败原因、重试 |
| `GET /api/*` | 内部 JSON | 供渐进增强用（轮询任务、续读 chunk） |

## 5. 关键界面决策

**检索结果卡** —— 排名是巨大的衬线数字（不是徽章），面包屑 `书名 › 卷一 › 总论`，
正文片段用 API 返回的 `highlights` 字符偏移做 `<mark>`（**不在前端做字符串匹配**，
偏移是后端算的，前端只负责切分）。底部一行小型度量：`score` / `rerank` / `cite id`。

**调试抽屉** —— 这是 kbsvc 的一等特性，前端必须让它可视化：左右并列 dense 与 sparse
两路的原始命中，中间画出融合后名次的**变化箭头**。看得见"哪一路把它捞上来的"，
调参才有依据。这也是整个前端最不像模板的地方。

**阅读器** —— 单栏、行高 1.9、字号可调三档。左侧栏外显示 ordinal 刻度，命中的 chunk
用朱砂色左边线标记。向下滚动自动续读下一批 chunk（`/api/chunks`），JS 失效时退化为
"下一段"链接。

**任务页** —— 状态用色语义化：completed 墨绿、failed 朱砂、running 靛青脉冲。
失败行内直接展开 `last_error` 并给重试按钮。

## 6. 目录结构

```
knowledge-web/
├── pyproject.toml
├── wsgi.py                     # gunicorn/waitress 入口
├── kbweb/
│   ├── __init__.py             # create_app 工厂
│   ├── config.py               # KBWEB_* 环境变量
│   ├── client.py               # KbClient：httpx 封装 kbsvc REST
│   ├── errors.py               # 后端不可达时的降级页
│   ├── filters.py              # Jinja 过滤器：高亮切分、面包屑、时长、字节数
│   ├── views/
│   │   ├── search.py  library.py  ingest.py  jobs.py  api.py
│   ├── templates/
│   │   ├── base.html  search.html  reader.html  library.html
│   │   ├── document.html  ingest.html  jobs.html
│   │   ├── partials/_result.html  _debug.html  _pill.html
│   │   └── errors/backend_down.html  404.html
│   └── static/
│       ├── css/tokens.css      # 设计令牌（色/字/间距/动效）
│       ├── css/app.css
│       └── js/{debug,reader,jobs}.js
└── tests/
```

## 7. 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `KBWEB_API_BASE` | `http://127.0.0.1:8077` | kbsvc 地址 |
| `KBWEB_API_KEY` | 空 | 转发到 kbsvc 的 Bearer token |
| `KBWEB_TIMEOUT` | `30` | HTTP 超时秒 |
| `KBWEB_PAGE_SIZE` | `10` | 默认 top_k |
| `KBWEB_SECRET_KEY` | 随机 | Flask session |
| `KBWEB_DEBUG_UI` | `true` | 是否允许前端请求 debug 负载 |

## 8. 安全

- API Key 只存在服务端配置，**不下发到浏览器**；所有请求由 Flask 代发
- 上传走 Flask → kbsvc，Flask 侧再做一次大小限制（`MAX_CONTENT_LENGTH`）
- 所有模板输出默认转义；`highlights` 渲染走白名单化的分段拼接，不用 `|safe` 拼原始 HTML
- 后端不可达时返回专门的降级页，不回显内部地址与堆栈

## 9. 不做什么（首版）

- 不做用户体系（kbsvc 的 API Key 就是边界）
- 不做写操作的富交互（删除/重建只在任务页给按钮，不做批量编辑器）
- 不做前端检索缓存（结果与调试信息必须与后端一致）

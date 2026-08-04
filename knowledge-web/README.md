# kbweb

[kbsvc](../knowledge-service/) 的 Flask 前端。服务端渲染、无构建步骤、无 CDN。

```
浏览器 ──▶ Flask (kbweb) ──HTTP──▶ kbsvc REST ──▶ Qdrant / PG / S3
```

## 为什么走 HTTP 而不是直接 import kbsvc

当前 `local` profile 把嵌入式 Qdrant 和常驻 worker 放在同一个 API 进程中，Web 仍是独立
表现层。Web 只走 HTTP，可以避免复制检索、任务和存储逻辑，也能无缝指向远程 kbsvc，
使 `local` 与 `server` 两种 profile 保持相同前后端边界。

顺带的好处：API Key 只存在 Flask 服务端，浏览器永远拿不到。

## 起步

推荐在仓库根目录用 `run.bat` 启动后端 API（内含 Qdrant 与 worker）和 Web。
单独开发前端时，先确保 kbsvc API 已运行，然后：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,prod]"
cp .env.example .env                 # 默认指向 127.0.0.1:8077，可直接用

flask --app wsgi run --port 5055     # 开发
waitress-serve --port 5055 wsgi:app  # 生产
```

打开 <http://127.0.0.1:5055>。

## 页面

| 路由 | 用途 |
|---|---|
| `/` | 检索：模式切换、来源/章节过滤、结果引用、调试抽屉 |
| `/read/<id>` | 阅读器：按段连续读，可锚定到检索命中的那一段 |
| `/library` | 书库：按来源分组，含索引统计 |
| `/library/<id>` | 文档详情：版本、段数、ACL、重建索引 |
| `/ingest` | 新建来源、上传单文件、按路径批量导入 |
| `/jobs` | 任务状态、失败原因、重试 |

## 设计取向

不做通用后台模板。方向是**古籍善本 / editorial**：

- **暖纸底色**，非纯白；深色主题是独立设计的"灯下绢本"，不是反色
- **CJK 衬线承载正文**——语料本身是文言文，衬线是语义正确的选择；界面 chrome 用无衬线，
  形成**字体对比**而非字号对比
- **朱砂红是语义色**：只出现在引用标记与查询命中高亮上，不做装饰
- **靛青只用于度量与调试**
- 排名用超大衬线数字，与正文构成 6:1 字号比——层次靠尺度对比，不靠加粗
- 结果间距刻意不均等，首条留白更大，形成阅读入口

完整设计说明见 [docs/01-frontend-design.md](docs/01-frontend-design.md)。

代码架构分析（依赖 DAG、复杂度、降级路径、前后端边界）见
[docs/02-code-architecture.md](docs/02-code-architecture.md) · [HTML 版](docs/02-code-architecture.html)。

## 检索调试抽屉

kbsvc 的 `debug` 负载是一等特性，前端必须让它可视化：左右并列 dense 与 sparse 两路的
原始命中，下方是 RRF 融合后的名次与各自贡献（`d#1 s#3 → 0.0328`）。

看得见"哪一路把它捞上来的"，调参才有依据。这也是这个前端最不像模板的地方。

## 渐进增强

JS 全部失效时，页面仍可检索、可翻页、可上传。三个原生脚本只做增强：

| 脚本 | 增强 | 退化行为 |
|---|---|---|
| `theme.js` | 明暗切换并记忆 | 跟随系统 `prefers-color-scheme` |
| `drawer.js` | 调试抽屉 | 抽屉内容仍在 DOM 中，可滚动查看 |
| `reader.js` | 滚动续读、原地追加 | 「续读下一段」变成普通链接 |
| `jobs.js` | 任务轮询（退避到 30s，隐藏标签页暂停） | 手动刷新 |

`reader.js` 用 `textContent` 写入正文，从不用 `innerHTML`。

## 一个必须知道的细节

HTML 复选框未勾选时**浏览器不提交该字段**，服务端无法区分"用户取消了勾选"和"表单没提交"。
所以检索表单带一个隐藏的 `f=1` 标记：有它则复选框缺省为 off，没有它（比如别人分享的
`/?q=...` 链接）则保持默认 on。这个坑有对应测试守着。

## 配置

见 [.env.example](.env.example)。要点：

- `KBWEB_API_KEY` 转发为 `Authorization: Bearer`，**不下发浏览器**
- `KBWEB_DEBUG_UI=false` 可在生产关闭调试负载
- `KBWEB_SECRET_KEY` 生产必须固定，否则每次重启 session 失效

## 开发

```bash
python -m pytest -q --cov     # 37 单元测试（默认跳过 e2e）
python -m ruff check .
```

后端在测试中全程被 `respx` 打桩，测试套件不碰网络。

## 端到端测试

50 个 Playwright 测试，驱动**真实 Chrome**：

```bash
uv pip install --python .venv -e ".[e2e]"
python -m pytest -m e2e tests/e2e -q          # 无头
KBWEB_E2E_HEADED=1 python -m pytest -m e2e tests/e2e   # 有头，可肉眼看
```

用 `channel="chrome"` 驱动本机已装的 Chrome，**不下载 Playwright 自带的 Chromium** ——
这台机器对大文件限速，而且测真实浏览器不是降级。后端由 `tests/e2e/stub_backend.py`
固定打桩，断言的是前端行为而非语料内容。

覆盖面：

| 组 | 覆盖 |
|---|---|
| 检索流 | 表单提交、命中高亮、深链到锚定段落、模式切换、复选框语义 |
| 交互 | 调试抽屉（Esc/scrim/焦点归还）、主题切换持久化、阅读器原地续读、任务重试 |
| 降级 | 关掉 JS 后阅读器仍可翻页 |
| 响应式 | 320/768/1024/1440 无横向溢出、栏位堆叠/并排、导航不竖裂 |
| 无障碍 | 单一 h1、键盘可达、焦点可见、序号对读屏隐藏、尊重 reduced-motion |
| 视觉 | 截图落到 `tests/e2e/screenshots/`，供人工审阅 |

每个测试还会断言页面**没有 console error 或未捕获异常**——静默的 JS 报错不算通过。

### 关于 Playwright MCP

仓库根的 `.mcp.json` 配了一个 `playwright-local`（`--browser chrome --headless`），
不需要 Chrome 扩展桥接。ecc 插件自带的那个跑在 `--extension` 模式，需要装
"Playwright MCP Bridge" 扩展才能用。**日常回归请跑上面的 pytest 套件**——它可复现、
进 CI，比让 agent 逐次点击可靠。

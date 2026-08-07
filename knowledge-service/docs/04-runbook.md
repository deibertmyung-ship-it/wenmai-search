# 运维手册

## 1. 本地起步（SQLite + LocalFS + Embedded Qdrant）

本地模式不需要 Docker。完成 Python 环境后，从仓库根目录使用统一脚本：

```powershell
cd knowledge-service
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,fastembed]"

cd ../knowledge-web
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev,prod]"

cd ..
run.bat               # API（嵌入式 Qdrant + worker）→ Web
run.bat status
run.bat stop
```

`run.bat` 首次启动会创建 SQLite 和嵌入式 Qdrant 集合。应用数据位于 `KB_DATA_DIR`：
```
.kbdata/
├── kbsvc.db        # SQLite 元数据
├── objects/        # 原始文件（按 hash 派生 key）
├── models/         # 本地 embedding / parser 模型
├── qdrant/         # 嵌入式 Qdrant 数据（稠密向量）
└── lexical/        # Tantivy 倒排索引（词法检索）
```

API 生命周期会启动一个后台 worker 线程，并与请求线程共享同一个 Qdrant 客户端和同一个
Tantivy 索引。停止 API 会同时停止 worker。不要在 API 运行期间另开 `kbsvc worker`、
`kbsvc search` 或本地 stdio MCP 进程访问同一 `.kbdata/`，否则第二个进程会因目录独占锁
失败——**Qdrant 与 Tantivy 都持锁**。

## 2. 服务端 Profile

compose 栈包含 postgres、qdrant、minio、api、worker、mcp 与 web 七个服务。

```bash
cd deploy
cp ../.env.example .env
```

`.env` 至少要填 `POSTGRES_PASSWORD` / `QDRANT_API_KEY` / `MINIO_ROOT_PASSWORD` /
`KBWEB_SECRET_KEY`。密码会被插值进 `postgresql://` URL，用十六进制串，别带 `@` `:` `/`。

`KB_MCP_API_KEY` 与 `KBWEB_API_KEY` 要等数据库建好才能签发，但 **compose 在执行任何命令前
会先插值整个文件**，`${VAR:?}` 缺失就直接报错。所以这两项先填占位串，签发后再替换：

```bash
docker compose up -d postgres qdrant minio
docker compose run --rm api kbsvc init                              # 建表 + 租户 + 空词法索引
docker compose run --rm api kbsvc issue-key mcp-agent --acl public  # 明文只显示这一次
docker compose run --rm api kbsvc issue-key kbweb --acl public
# 把两个明文写回 .env 的 KB_MCP_API_KEY / KBWEB_API_KEY
```

起服务前先单独预热一次嵌入权重。api 与 worker 共享同一个模型缓存目录，同时启动会并发下载
同一份 ONNX：

```bash
docker compose run --rm api python -c "from kbsvc.embedding import get_dense_embedder; get_dense_embedder().embed_query('warmup')"
docker compose up -d api worker mcp web
```

### worker 数量的上限是 1

任务表的租约机制本身支持水平扩展，但**词法索引挡在前面**：Tantivy 是进程内库、持目录独占锁，
而 worker 在 `ingest/worker.py` 里内联写词法索引。第二个 worker 副本抢不到 writer 会失败。

```bash
docker compose up -d --scale worker=4      # 今天会挂，不要这么做
```

要真正扩 worker，得先把词法写入收敛到单一写入者背后（独立的 lexical-writer 服务或一个
专门的任务类型），在那之前 `KB_WORKER_REPLICAS` 只能是 1。

### 共享卷不是可选项

api / worker / mcp 三类容器挂同一个 `kbdata` 卷到 `/data`（`KB_DATA_DIR`），里面是词法索引和
模型缓存。**不挂共享卷时不会报错**：每个容器各自在自己的层里建一个空的 Tantivy 索引，worker
写它自己那份，api 读它自己那份空的，于是 hybrid 静默退化成纯 dense，`mode=sparse` 恒返回空。
`/v1/stats` 的 `lexical_docs` 与 `chunks` 对不上是这个故障的信号。

只读容器（api / mcp）按秒级节流 reload 索引来看见 worker 的提交，见
`lexical/tantivy_store.py` 的 `_refresh_reader`。

### 语料导入

`book/` 以 `/corpus:ro` 挂进 api 与 worker。用 CLI 导入时**必须加 `--no-run-worker`**——
CLI 自带的 drain 会去抢 worker 容器已持有的 Tantivy 写锁：

```bash
docker compose run --rm api kbsvc ingest /corpus --source guji \
    --patterns "*.txt,*.md" --no-run-worker
```

登记完由 worker 容器消费队列。进度：

```bash
docker compose exec postgres psql -U kbsvc -d kbsvc \
    -c "select state, count(*) from ingest_job group by state"
```

> Windows 的 git bash 会把 `/corpus` 这类参数改写成 Windows 路径，导致 `registered=0` 且
> 不报错。加 `MSYS_NO_PATHCONV=1` 前缀，或改用 PowerShell。

## 3. 导入方式对照

| 方式 | 场景 | 命令 |
|---|---|---|
| CLI | 服务器本地大批量 | `kbsvc ingest /data/books --source guji --patterns "*.pdf"` |
| `POST /v1/ingest/path` | 远程触发、路径对服务端可见 | 见 [03-api.md](03-api.md) |
| `POST /v1/ingest/upload` | 单文件、客户端持有内容 | multipart |

三者最终都写同一张 `ingest_job` 表，由同一个 worker 消费。

## 4. 常见排障

**任务卡在 `pending`** — 先执行 `run.bat status`。本地 worker 与 API 共用
`logs/kbsvc.log`；执行 `run.bat restart` 会同时重启 API 内置 worker 与 Web。server profile
查看 `docker compose logs worker`。

**任务 `failed`** — 查错误后重试：
```bash
curl -s localhost:8077/v1/jobs?state=failed | jq '.[] | {id, last_error}'
curl -X POST localhost:8077/v1/jobs/<id>/retry
```

**PDF/Office 解析失败** — Docling、Unstructured 与 Marker 已默认安装；先执行
`python -c "import docling, unstructured, marker"` 检查部署完整性。若 import 正常，查看 job 的
`last_error` 判断是否缺少模型权重或系统组件；链路会按 Docling → Unstructured → Marker 自动降级。
实际生效的解析器记录在 `document_version.parser`。

**检索召回差** —
1. 先 `debug: true` 看 `retrievers.dense` 与 `retrievers.sparse` 各自命中了什么。
2. 只有 sparse 有结果 → dense 用的还是 `hash` provider，换 `fastembed` 或 `openai`。
3. 只有 dense 有结果 → 查询词在语料里不以原字出现，属正常。
4. 两路都为空 → 检查 `filter`：`current_only` 与 `acl` 是最常见的误杀。

**索引目录被占用** — 嵌入式 Qdrant 与 Tantivy 各自只允许一个进程持有 `.kbdata/qdrant`、
`.kbdata/lexical`。先执行 `run.bat stop`，并关闭仍在运行的 `kbsvc search`、`kbsvc worker`
或 stdio MCP 进程，再重新启动。

**换了 embedding 模型** — 见第 8 节，使用 `run.bat reembed` 或 `kbsvc reembed`，不需要
删除原始文件或重新解析文档。

**`lexical_docs` 与 `chunks` 对不上** — 两个索引漂移了。停止 API 后跑
`kbsvc rebuild-lexical`（22,659 段约 21 秒），它只重建词法索引，不动稠密向量。

**精排比预期慢** — 存量 chunk 的 `analyzed` 列为空，精排在实时分词。跑
`kbsvc backfill-analyzed`（22,345 段约 42 秒），只写元数据库，不动两个索引。改了
`lexical/tokenizer.py` 之后要加 `--force`，否则存量值仍是旧分词器的产物。

**Windows 上词法索引写入报 `PermissionDenied`（`.pos` / `.fieldnorm`）** — Tantivy 多线程
写入与按访问扫描的安全软件抢文件句柄。默认 `KB_LEXICAL_WRITER_THREADS=1` 已规避；若被改大
过，调回 1，或给 `KB_DATA_DIR` 加杀软排除目录。

## 5. 一致性保证

- 更新：新版本索引成功后，旧版本 chunk 从元数据库与向量库同时删除，并写 `version_superseded` 事件。
- 删除：软删 document + 物理删 chunk/向量 + `document_deleted` 事件。
- 中断：worker 崩溃 → 租约到期 → 另一 worker 接管；chunk_id 幂等，重跑不产生重复点。
- 两个索引：稠密（Qdrant）与词法（Tantivy）在同一次 worker 执行里一起写、一起删。词法侧
  在写入前先按 `version_id` 整版本删除，与 SQL 侧 `replace_chunks` 同语义，重跑不残留旧
  切分。语料统计由 Tantivy 自己持有——**统计与索引同源，不会漂移**（旧版本用两张表手工
  维护，是可能对不上的）。
- 校验：`/v1/stats` 的 `chunks` / `vector_points` / `lexical_docs` 三者应相等。

## 6. 备份

| 数据 | 备份对象 |
|---|---|
| 元数据 | `pg_dump` / 复制 `kbsvc.db` |
| 原始文件 | S3 bucket / `objects/` 目录 |
| 稠密向量 | server 使用 Qdrant snapshot；local 停止 API 后复制 `qdrant/` |
| 词法索引 | 停止 API 后复制 `lexical/`，或干脆不备份 |

原始文件是唯一不可再生的部分——向量与 chunk 都能从它重建。优先保它。

词法索引优先级最低：它完全可以从 SQLite 的 chunk 表用 `kbsvc rebuild-lexical` 在几十秒内
重建，备份它的性价比低于备份 `kbsvc.db`。

## 7. MCP 客户端接入

**本地 stdio**（Claude Code / Claude Desktop）：
```json
{
  "mcpServers": {
    "kbsvc": {
      "command": "python",
      "args": ["-m", "kbsvc.cli", "mcp", "--transport", "stdio"],
      "env": {
        "KB_DATA_DIR": "C:/path/to/knowledge-service/.kbdata",
        "KB_QDRANT_URL": "",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

嵌入式目录不能跨进程共享：运行上述 stdio MCP 前必须先 `run.bat stop`。若需要 Web/API 与
stdio MCP 同时在线，应改用 server profile 的 Qdrant Server，或让客户端连接远程 MCP。

**远程 streamable-http**：
```json
{
  "mcpServers": {
    "kbsvc": { "type": "http", "url": "https://kb.example.com/mcp" }
  }
}
```
服务端以 `KB_MCP_API_KEY` 绑定租户与 ACL；客户端无法自选租户。

## 8. 切换 embedding 模型

```bash
# 1) 装 extra
pip install '.[fastembed]'

# 2) 切换 provider
export KB_DENSE_PROVIDER=fastembed
export KB_DENSE_MODEL=BAAI/bge-small-zh-v1.5     # 512 维，92MB

# 3) 重建向量（不重新解析原文件）
# Windows 本地一键方式：从仓库根目录运行 run.bat reembed
kbsvc reembed --batch-size 256
```

`reembed` 从**已存的 chunk 表**重新计算向量：chunk_id、字符偏移、heading_path 都与模型
无关，因此不需要重跑解析与切分。耗时取决于模型、CPU 和 Qdrant 写入速度。它同时重建词法
索引，让两路始终描述同一份语料。

只有改了切分参数（`KB_CHUNK_*`）才需要走 `reindex`——那会从对象存储里的原始文件重新解析。

### 改了 KB_NORMALIZE_CJK

字形归一化同时影响两路索引：词法侧决定 token，稠密侧决定送去嵌入的文本。改了它必须
`kbsvc reembed`（两路一起重建），只跑 `rebuild-lexical` 会让两路对同一个查询理解不一致。

往 `normalize.py` 的 `_EXTRA`（补充旧字形）或 `_PROTECTED`（保护领域用字）里加条目，同理。

### 只重建词法索引

词法索引与嵌入模型无关。从旧版本（自研 BM25 + Qdrant 稀疏向量）升级，或 `/v1/stats` 显示
`lexical_docs` 与 `chunks` 不一致时，用：

```bash
kbsvc rebuild-lexical --batch-size 512      # 22,659 段约 21 秒
```

它不碰稠密向量——为了修一个倒排索引而把整个语料重新过一遍嵌入模型是几分钟的浪费。同样
需要先 `run.bat stop`：Tantivy 持目录锁。

### 模型下载受限时

`fastembed` 默认从 HuggingFace 拉权重。若该网络对 HF 限速或重置连接，可自行把 ONNX 放到
缓存目录，kbsvc 会直接使用并自动进入离线模式：

```
$KB_DATA_DIR/models/fast-bge-small-zh-v1.5/
├── model_optimized.onnx      # 必需
├── config.json
├── tokenizer.json
├── tokenizer_config.json
└── special_tokens_map.json
```

ModelScope 上的 `Maiteka/bge-small-zh-v1.5-onnx` 提供同一份权重，国内网络通常快得多：

```bash
curl -L -o model_optimized.onnx \
  "https://modelscope.cn/api/v1/models/Maiteka/bge-small-zh-v1.5-onnx/repo?Revision=master&FilePath=model.onnx"
```

权重就位后 kbsvc 会自动设置 `HF_HUB_OFFLINE=1`——否则 fastembed 每次启动都会去探
Hub，在受限网络上会挂住数分钟而不是快速失败。

### 换模型必须重建

向量维度与向量空间在建集合时就固定了。`reembed` 默认会重建当前 Qdrant 集合；
`--keep-collection` 仅在维度不变且执行断点续跑时使用。只改模型配置而不重建，会导致
语义空间不一致或在 upsert 时出现维度不匹配。

### reembed 期间索引不可用

`reembed` 会先删掉集合再逐批写入，**重建过程中检索返回空结果**。本地 `run.bat reembed`
会先停止 API（包含 worker）和 Web，完成后自动恢复全部服务，避免并发写入旧集合。

### 中途被打断怎么办

`reembed` 按 chunk_id 升序流式处理，因此断点可续。运行结束（或日志最后一行）会给出
`last_chunk_id=...`，据此续跑：

```bash
kbsvc reembed --batch-size 256 --resume-after <last_chunk_id>
```

**词法索引不参与断点续跑。** 它在整轮结束时一次性提交（见
[05-performance.md](05-performance.md) 的「Windows 与写入线程」），所以被打断后会停在上一次
提交的状态。续跑完稠密一路之后补一句即可：

```bash
kbsvc rebuild-lexical            # 22,659 段约 21 秒
```

用 `/v1/stats` 确认 `chunks` / `vector_points` / `lexical_docs` 三者相等。

如果日志丢了，可以从向量库的点数反推——已写入的正是 id 升序的前 N 个：

```bash
python - <<'PY'
import sqlite3, os
from kbsvc.vector import get_vector_store
n = get_vector_store().count("default")
c = sqlite3.connect(f"file:{os.environ['KB_DATA_DIR']}/kbsvc.db?mode=ro", uri=True)
print(c.execute(
    "select id from chunk where tenant_id=? order by id limit 1 offset ?", ("default", n - 1)
).fetchone()[0])
PY
```

`--resume-after` 隐含保留集合（不会重建），进度显示的是**全库绝对位置**而不是本次增量。

生产环境要零停机，用影子集合切换：

```bash
# 1) 写入一个新集合，老集合继续对外服务
KB_QDRANT_COLLECTION=kb_chunks_v2 kbsvc reembed --batch-size 256

# 2) 确认新集合可用
KB_QDRANT_COLLECTION=kb_chunks_v2 kbsvc search "贼克" --top-k 3

# 3) 改配置指向新集合并重启（server profile 下是滚动重启）
export KB_QDRANT_COLLECTION=kb_chunks_v2
```

server profile 可进一步实现集合别名原子切换；local 嵌入式模式不支持零停机切换，
`run.bat reembed` 会先停止 API 与 Web，再采用更简单可靠的停机重建策略。

## 9. 抄袭检测运维

仅 PostgreSQL，默认关闭。两个开关是独立的，这样历史语料可以在接口关闭时先建好：

| 变量 | 作用 |
|---|---|
| `KB_PLAG_INDEXING_ENABLED` | 后台建投影（入库钩子 + 回填） |
| `KB_PLAG_ENABLED` | 对外开放 HTTP 接口 |

### 首次启用顺序

```powershell
kbsvc plagiarism init          # 建 9 张表与 GIN 索引，幂等
# 设 KB_PLAG_INDEXING_ENABLED=true，重启 worker
kbsvc plagiarism backfill      # 只登记任务，不计算
kbsvc plagiarism-worker        # 独立进程，实际构建
kbsvc plagiarism rebuild-df    # 重算指纹频率并 ANALYZE
kbsvc plagiarism status        # 确认覆盖率 100%
# 验收通过后，人工设 KB_PLAG_ENABLED=true
```

`backfill` 只登记幂等任务，**可随时中断、可重复运行**——大语料回填是数小时的
指纹计算，放进 CLI 进程会让它不可恢复也不可观察。

### 抄袭 worker 必须独立进程

```powershell
kbsvc plagiarism-worker            # 常驻
kbsvc plagiarism-worker --once     # 排空后退出，适合 cron 或调试
```

不要指望入库 worker 顺带做这件事：对齐是 CPU 密集的，同进程会拖住解析、嵌入与
索引写入。API 的 lifespan 也不会启动它。

worker 先排空语料任务再处理检测任务——没有投影的文档会阻塞**每一个**检测，
让构建排在检测后面等于让一个慢检测把整个语料变得不可检。

### 常见故障

**`/readyz` 报 `plagiarism_worker: no live worker`** — 没有抄袭 worker 在跑，
或它的心跳超过 120 秒未更新。检测仍会被接受，但永远排队。

**`/readyz` 报 `plagiarism_schema: gin index missing`** — 表在但 GIN 索引没了。
候选检索**没有 Python 扫描兜底**（ADR-0001 有意如此），失去索引的表现是无上限
地变慢而不是报错。跑 `kbsvc plagiarism init` 补建。

**创建检测一直返回 409 `plagiarism_corpus_not_ready`** — 有文档还没建成投影。
`kbsvc plagiarism status` 看 pending/failed 计数；failed 的用
`kbsvc plagiarism backfill` 重新登记（失败任务会被重新排队）。

**改了 `KB_PLAG_*` 算法参数后所有检测被拒** — 算法配置哈希变了，存量投影全部
失效。跑 `kbsvc plagiarism rebuild` 全量重建，完成前拒绝创建检测是设计如此，
不是故障。只有分块窗口、重叠、k-gram、winnow window 参与哈希；检索与对齐的
调参不需要重建。

**检测很慢或召回差** — 先确认 `kbsvc plagiarism rebuild-df` 跑过。指纹频率表
为空时停用词过滤失效，高频套语会淹没真实命中。该命令同时执行 `ANALYZE`——
回填期间表大小变化几个数量级，过期的行数估计正是让规划器放弃 GIN 索引的原因。

**报告返回 409 `report_visibility_changed`** — 报告里某个来源对当前调用方已
不可见（ACL 变更或文档删除）。这是有意的：不部分展示，也不重算旧报告。

### 清理

```powershell
kbsvc plagiarism cleanup      # 删过期任务、原文、结果、事件与停用投影
```

默认保留 30 天（`KB_PLAG_RETENTION_DAYS`）。停用投影只有在没有活动检测的快照
引用它时才会被删。**这只管 `plag_*` 数据**——对象存储里的原始文件不受影响。

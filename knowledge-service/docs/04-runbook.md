# 运维手册

## 1. 本地起步（无 Docker）

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"

export KB_DATA_DIR="$PWD/.kbdata"
python -m kbsvc.cli init
python -m kbsvc.cli ingest ../book --source guji --patterns "*.txt,*.md"
python -m kbsvc.cli search "贼克如何取用神" --top-k 5
```

数据全部落在 `KB_DATA_DIR`：
```
.kbdata/
├── kbsvc.db        # SQLite 元数据
├── objects/        # 原始文件（按 hash 派生 key）
└── qdrant/         # 嵌入式向量库
```
删除该目录 = 完全重置。

## 2. 服务端 Profile

```bash
cd deploy
cp ../.env.example .env    # 填 POSTGRES_PASSWORD / QDRANT_API_KEY / MINIO_ROOT_PASSWORD
docker compose up -d postgres qdrant minio
docker compose run --rm api kbsvc init
docker compose run --rm api kbsvc issue-key mcp-agent --acl public   # 记下明文
# 写入 .env 的 KB_MCP_API_KEY 后
docker compose up -d api worker mcp
docker compose up -d --scale worker=4      # 按吞吐扩 worker
```

## 3. 导入方式对照

| 方式 | 场景 | 命令 |
|---|---|---|
| CLI | 服务器本地大批量 | `kbsvc ingest /data/books --source guji --patterns "*.pdf"` |
| `POST /v1/ingest/path` | 远程触发、路径对服务端可见 | 见 [03-api.md](03-api.md) |
| `POST /v1/ingest/upload` | 单文件、客户端持有内容 | multipart |

三者最终都写同一张 `ingest_job` 表，由同一个 worker 消费。

## 4. 常见排障

**任务卡在 `pending`** — worker 没起。`kbsvc worker --once` 手动排空，或看 `docker compose logs worker`。

**任务 `failed`** — 查错误后重试：
```bash
curl -s localhost:8077/v1/jobs?state=failed | jq '.[] | {id, last_error}'
curl -X POST localhost:8077/v1/jobs/<id>/retry
```

**PDF 解析失败** — 默认没装 docling。`pip install '.[docling]'`；仍失败则链路会自动降级到 unstructured / marker（需各自安装）。实际生效的解析器记录在 `document_version.parser`。

**检索召回差** —
1. 先 `debug: true` 看 `retrievers.dense` 与 `retrievers.sparse` 各自命中了什么。
2. 只有 sparse 有结果 → dense 用的还是 `hash` provider，换 `fastembed` 或 `openai`。
3. 只有 dense 有结果 → 查询词在语料里不以原字出现，属正常。
4. 两路都为空 → 检查 `filter`：`current_only` 与 `acl` 是最常见的误杀。

**embedded Qdrant 报锁冲突** — 同一 `KB_DATA_DIR` 只能有一个进程持有。要同时跑 API 与 worker，请切到 server profile（Qdrant 服务模式）。

**换了 embedding 模型** — 见第 8 节，用 ，不需要手工删目录。```bash
rm -rf "$KB_DATA_DIR/qdrant"          # 或 server 上删 collection
# 逐文档重新入队（reindex job 复用已存的原始字节，不需要重新上传）
```

## 5. 一致性保证

- 更新：新版本索引成功后，旧版本 chunk 从元数据库与向量库同时删除，并写 `version_superseded` 事件。
- 删除：软删 document + 物理删 chunk/向量 + `document_deleted` 事件。
- 中断：worker 崩溃 → 租约到期 → 另一 worker 接管；chunk_id 幂等，重跑不产生重复点。
- BM25 词表：`term_stat` / `corpus_stat` 随 chunk 增删同步加减，索引侧与查询侧共用，IDF 永远一致。

## 6. 备份

| 数据 | 备份对象 |
|---|---|
| 元数据 | `pg_dump` / 复制 `kbsvc.db` |
| 原始文件 | S3 bucket / `objects/` 目录 |
| 向量 | Qdrant snapshot / `qdrant/` 目录 |

原始文件是唯一不可再生的部分——向量与 chunk 都能从它重建。优先保它。

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
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

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
kbsvc reembed --batch-size 256
```

`reembed` 从**已存的 chunk 表**重新计算向量：chunk_id、字符偏移、heading_path 都与模型
无关，因此不需要重跑解析与切分。202 部古籍 / 22350 段的实测约 5 分钟。

只有改了切分参数（`KB_CHUNK_*`）才需要走 `reindex`——那会从对象存储里的原始文件重新解析。

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

向量维度与向量空间在建集合时就固定了。`reembed` 默认会重建集合（`--keep-collection`
仅在维度不变时可用）。嵌入式 Qdrant 下，kbsvc 会物理删除集合目录——`delete_collection`
在本地模式只改配置、不清向量存储，不删目录就会在 upsert 时报维度不匹配。

### reembed 期间索引不可用

`reembed` 会先删掉集合再逐批写入，**重建过程中检索返回空结果**。22350 段实测需要
30–60 分钟（嵌入只占约 30 秒，其余是嵌入式 Qdrant 的 upsert，且随集合增大而变慢）。

### 中途被打断怎么办

`reembed` 按 chunk_id 升序流式处理，因此断点可续。运行结束（或日志最后一行）会给出
`last_chunk_id=...`，据此续跑：

```bash
kbsvc reembed --batch-size 256 --resume-after <last_chunk_id>
```

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

首版没有把这套切换做成一条命令——它需要集合别名，而别名只有 Qdrant 服务端支持，
嵌入式模式没有。`local` profile 下只能接受这段停机。

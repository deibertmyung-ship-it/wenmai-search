# 会话总结：ADR-0008 生产环境切换至 pgvector / pg_search

- 日期：2026-08-19
- 分支：`docs/adr-0008-storage-consolidation`
- 工单：`.scratch/storage-consolidation/issues/12-production-cutover.md`（Status: ready-for-human，14 天观察期进行中）
- 授权：用户明确批准「11号票已确认，请现在真实切到新架构上pg方案」

## 一、目标

把生产检索后端从 Qdrant（稠密）+ Tantivy（稀疏）真实切换到 ADR-0008 的 PG 原生栈：

- 稠密：pgvector HNSW
- 稀疏：pg_search（ParadeDB BM25）
- 融合：混合 RRF + 词法重排

旧的 Qdrant / Tantivy 数据和 PG16 卷全部保留，作为回滚目标，不在本次清理。

## 二、完成的事项

### 1. 生产配置切换（`knowledge-service/deploy/docker-compose.yml`）

在 `kb_env` 锚点新增：

```yaml
KB_VECTOR_BACKEND: ${KB_VECTOR_BACKEND:-pgvector}
KB_LEXICAL_BACKEND: ${KB_LEXICAL_BACKEND:-pg-search}
```

配套变更（部分来自上一会话，本次仍在工作树）：

- ParadeDB 镜像按 digest 固定：`sha256:dca046b8a0ddd070c368d5254cbb7f596b3b7bc67bc2efa7c0fbe881087883bb`
- 数据卷切换到 `pgdata_pg17`；旧 `pgdata`（PG16）保留声明但不再引用，留作回滚
- worker 副本注释更新：pg_search 下不再有 Tantivy 单写者锁；副本数暂保持默认 1，待观察期验证吞吐后再提升
- qdrant 容器仍接线、仍运行，作为即时回滚目标

### 2. 修复稠密检索在只读进程中静默返回 0 命中

**现象**：切换后真实 HTTP 搜索 `dense=0 / sparse=12`，稠密半侧稳定空返回；但同一方法在一次性脚本里调用却正常。Qdrant 日志前后行数持平（2517→2517），证明请求确实走到了 pgvector 而非回退到 Qdrant。

**错误的初步假设（已如实订正）**：怀疑是连接池里 prepared statement / `SET LOCAL` 的事务作用域问题。证伪——原始 SQL 与一次性脚本都能返回数据，且 `dense_search` 计时里混入了 embedder ONNX 冷启动。

**真实根因**：

- `PgVectorStore._dim` 是一个 Python 实例上的缓存，记录的是「数据库里 `chunk.embedding` 列的维度」这一**数据库事实**；
- 整个代码库里只有 `ensure_collection()` 会写 `_dim`；
- `search_dense()` 开头有 `if self._dim is None: return []`；
- API 进程的 lifespan（`api/app.py`）把 store 初始化（唯一的 `ensure_collection()` 调用点）关在 `if settings.run_api_worker:` 后面；
- server profile 下 `KB_API_WORKER_ENABLED=false`，于是 API 进程里 `_dim` 永远是 `None`，稠密检索直接早退成空列表——**静默失败，不报错**。
- A/B 对比脚本之所以没抓到，是因为它以一次性脚本方式运行，启动路径上显式调了 `ensure_collection()`。

**修复**（在 store 层修，不在调用点打补丁——调用点补丁在 79ab849 已经失败过一次）：

```python
# pgvector_store.py search_dense() 开头
if self._dim is None:
    self._dim = get_pgvector_dimension(self._engine)
    if self._dim is None:
        return []  # chunk.embedding 列确实还不存在
```

`get_pgvector_dimension(engine)` 是早已存在的目录内省函数（解析 `format_type()`），直接复用。sqlite-vec 路径有完全相同的缺陷，同样修复（`get_vec0_dimension`，内省 `sqlite_master`），CLI `kbsvc search` 也从中受益。

### 3. 回归测试

- `tests/test_pgvector_store.py` 新增 `TestReadOnlyProcessSelfSufficiency`：writer 建表+upsert 后，new 一个**没有**调 `ensure_collection` 的 reader，断言 `reader._dim is None` 初始成立、随后 `search_dense` 能返回种子点。
- `tests/test_sqlite_vec_store.py` 新增平行测试类。
- pgvector 模块：42 passed / 1 skipped
- sqlite-vec 模块：22 passed / 1 skipped
- 全套件：**547 passed / 2 failed / 2 skipped**（181.69s）
  - 失败 1：`test_separator_runs_are_not_reported_as_reuse`（`InputTooShortError`）——经 `git stash` 验证在原始代码上也失败，与本次改动无关，是预先存在的问题。
  - 失败 2：`TestNarrowFilterFaster.test_narrow_filter_faster_than_no_filter`——sqlite 计时断言（1.5x 比率，实测 1.57/1.67）。该测试早有 de-flake 提交 4fa39d5 处理同样的「全套件失败、隔离通过」模式；`--deselect` 掉本次新增测试后，修复代码仍在而该用例通过，证明是**新测试扰动了计时**而非修复引入的回归。按 11 号票规则**未放宽阈值**，留待人判。

**测试隔离红线**：所有测试连 `kbsvc-dev-paradedb`（端口 55433）的 `kbsvc_test` 库（本会话创建，密码 `kbsvc_dev_pw`），**绝不连生产**——因为 `_fast_schema` / `recreate_collection` 会 DROP `chunk.embedding`。

## 三、过程中踩到的基础设施问题

1. **Docker daemon 启动**：会话开始时 Docker Desktop 未运行，启动后轮询直到就绪。
2. **compose 误判 postgres / 名称冲突**：`docker compose up` 试图 Recreate 被改名的 pg16-rollback 容器（它还带着 compose label）来占用 `kbsvc-postgres-1`，而手动 `docker run` 的新容器已经占了这个名字。compose 删除了回滚容器的外壳，但数据卷 `kbsvc_pgdata` 完好。之后一律用 `--no-deps`，数据容器手动管理。
3. **`failed to resolve host 'postgres'`**：手动启动的 postgres 不在 `kbsvc_default` 网络上。`docker network connect --alias postgres kbsvc_default kbsvc-postgres-1` 后重启应用容器解决。
4. **2026-08-09 旧镜像不含 ADR-0008 代码**：`get_settings().vector_backend` 直接 `AttributeError`；pydantic-settings `extra="ignore"` 把不认识的 `KB_VECTOR_BACKEND` 静默丢弃；第一次「成功」的搜索其实打到了 Qdrant（Qdrant 日志证实）。教训：重建全部 4 个镜像后，要用 `docker run --entrypoint sh ... grep` 验证镜像内代码、比对运行中容器的 image ID，再敢相信部署。
5. **后台构建首次在 590s 超时被杀**：pyproject.toml 变更使 torch 层缓存失效。改用 `nohup ... & disown`  detached 写到日志文件，轮询等待。
6. **daemon 卡在移除三个 Dead 容器 15–20 分钟**：`docker system df` 超时。提议 `wsl --terminate docker-desktop` 被自动模式拦截，询问用户后用户选择「再等一等，暂不重启」，随后自愈。**用户反馈：未经明确许可不得重启 Docker Desktop。**
7. **Git Bash 吞中文**：curl JSON 体里的中文被破坏，改为写到文件用 `--data-binary @file`。
8. **`docker logs --since`  naive 时间戳**：按本地 UTC+8 解释，拉出几小时前的旧日志。改用前后行数对比。

## 四、验证结论

- 设置解析正确：运行中容器 `KB_VECTOR_BACKEND=pgvector`、`KB_LEXICAL_BACKEND=pg-search`。
- 运行镜像就是修复后重建的镜像（api `sha256:735f1f3732af...`），容器内 grep 可见修复代码。
- 真实 HTTP 搜索：稠密 `dense=12`、稀疏 `sparse=12`（修复前 dense=0）。
- Qdrant 零流量证据：日志行数搜索前后 2517→2517。
- CLI 路径同样修复。
- postgres 容器 ID `7c5ba7099de1` 在整个切换过程中保持不变。

## 五、未决事项（不自行决定）

1. **提交**：本次 5 个文件（+136/-12）仍在工作树，尚未 `git commit`：
   - `knowledge-service/src/kbsvc/vector/pgvector_store.py`
   - `knowledge-service/src/kbsvc/vector/sqlite_vec_store.py`
   - `knowledge-service/tests/test_pgvector_store.py`
   - `knowledge-service/tests/test_sqlite_vec_store.py`
   - `knowledge-service/deploy/docker-compose.yml`
   - 建议拆两个提交：(a) `fix: pgvector dense search silently returned empty for read-only processes (ADR-0008 ticket 12)`；(b) `chore: cut production over to pgvector/pg-search (ADR-0008 ticket 12)`。**不得**加入 QA/*、`docs/05-performance.md`、`.codebase-memory/`、`out/`、`{const` 等无关文件。
2. **flaky 计时测试 `TestNarrowFilterFaster`**：是否接受现状 / 把新测试挪到它之后 / 重新校准——留待人判，不单方面放宽阈值。
3. **14 天观察期（自 2026-08-19 起）**：盯标题级查询、P95 延迟、摄入吞吐、磁盘。
4. **多 worker 吞吐验证**：`KB_WORKER_REPLICAS` 仍为 1；pg_search 下 Tantivy 单写者锁已消失，提升副本并测量是 ADR 的核心承诺，在观察期内完成。
5. **清理（不是现在）**：`kbsvc-dev-paradedb` 测试容器与 `kbsvc_test` 库可留作重跑测试；旧 PG16 卷/容器与 Qdrant 数据保留到 13 号票。

## 六、纪律备注

- A/B 通过/否决标准是否软化是**人的判断**，不自行决定。
- 检索后端翻转是**人的决策**，本次凭用户明确授权执行。
- 不连生产库跑测试；不未经许可重启 Docker Desktop；不放宽测试阈值；不提交 `.env` 或在命令里暴露密钥；始终用中文回复。
- 错误的根因假设已在工单 12 中明确订正记录，未隐藏。

# Docker 开发模式

`docker-compose.yml` 是生产部署配置：源码复制进镜像，便于审计、回滚和复现。
`docker-compose.dev.yml` 是开发覆盖配置：源码以只读 bind mount 挂载进容器，数据库、
Qdrant、MinIO 和 Tantivy 数据仍使用生产配置中的命名卷。

开发覆盖会开启 Flask/Uvicorn debug reload，只适用于受信任的本机开发环境；不要把这套
覆盖文件或端口直接暴露到公网生产环境。

## 首次启动

在本目录执行：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build `
  api worker plagiarism-worker mcp web
```

两个 Compose 文件会合并，第二个文件只覆盖开发所需的命令、环境变量和源码挂载。
基础设施容器不会被 `--no-deps` 重启。

## 修改代码后的行为

- `knowledge-service/src`：API 使用 Uvicorn `--reload` 自动重载。
- `knowledge-web/kbweb`、`wsgi.py`：Web 使用 Flask debug reloader 自动重载。
- `worker`、`plagiarism-worker`、`mcp`：源码已挂载，但这些长驻进程不会自动重新导入 Python 模块；修改后重启它们：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart `
  worker plagiarism-worker mcp
```

重启不会丢失命名卷中的数据。

## 依赖、配置和数据库变更

修改 `pyproject.toml`、Dockerfile 或依赖版本时，需要重新构建：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --no-deps `
  api worker plagiarism-worker mcp web
```

修改环境变量但未修改镜像内容时，重建容器即可：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --no-deps --force-recreate `
  api worker plagiarism-worker mcp web
```

修改查重数据库结构时，先执行应用迁移：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --no-deps `
  api kbsvc plagiarism init
```

修改指纹、规范化、分块或算法版本时，还需要重新生成查重投影和 DF：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --no-deps `
  api kbsvc plagiarism rebuild
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --no-deps `
  api kbsvc plagiarism rebuild-df
```

## 停止与验证

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps -a
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs --tail=100 api web
Invoke-WebRequest http://127.0.0.1:8077/healthz
Invoke-WebRequest http://127.0.0.1:5055/
```

停止开发应用而不删除数据：

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml stop `
  api worker plagiarism-worker mcp web
```

不要使用 `docker compose down -v`，它会删除命名卷。

## 生产部署

生产环境只使用基础 Compose 文件，不挂载宿主机源码：

```powershell
docker compose -f docker-compose.yml up -d --build --force-recreate `
  api worker plagiarism-worker mcp web
```

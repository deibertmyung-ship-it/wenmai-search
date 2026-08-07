# 文脉 Wenmai Search

自托管的古籍混合检索系统。两个可独立部署的包：

- `knowledge-service` — 后端。解析、切块、嵌入、Qdrant + Tantivy 混合检索、REST 与 MCP 接口。
- `knowledge-web` — Web 前端。经由 REST 调用后端，不直连任何存储。

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature-slug>/` in this repo — not GitHub Issues, because this repo is public and in-flight work should not appear on it by default. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name, recorded as a `Status:` line in the issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Multi-context — one context per package, with package-scoped ADRs under `<package>/docs/adr/` and system-wide ones at the repo root. See `docs/agents/domain.md`.

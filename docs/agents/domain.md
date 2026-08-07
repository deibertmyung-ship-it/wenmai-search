# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This repo is **multi-context**: two independently deployable packages, each with its own docs tree.

## Before exploring, read these

- **`CONTEXT-MAP.md`** at the repo root if it exists — it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`CONTEXT.md`** inside the relevant package.
- **`<package>/docs/adr/`** — read ADRs that touch the area you're about to work in.
- **`docs/adr/`** at the repo root — system-wide decisions that span both packages.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

The contexts are the two packages at the repo root, not directories under a `src/`:

```
/
├── CONTEXT-MAP.md                     ← not yet created
├── docs/
│   ├── agents/                        ← this directory
│   └── adr/                           ← system-wide decisions (not yet created)
├── knowledge-service/                 ← context: 后端检索服务
│   ├── CONTEXT.md                     ← not yet created
│   └── docs/
│       ├── adr/                       ← 5 ADRs live here today
│       ├── plans/                     ← file-level implementation plans
│       └── specs/                     ← accepted formal specs
└── knowledge-web/                     ← context: Web 前端
    ├── CONTEXT.md                     ← not yet created
    └── docs/
```

The ADRs already living in `knowledge-service/docs/adr/` are the reason this repo is set up multi-context rather than single-context: the decisions are package-scoped, and moving them to the root would misrepresent their reach.

`knowledge-service/docs/adr/README.md` is the ADR index and states the house format.

## Which context owns a decision

- Touches only retrieval, ingest, storage, or the REST/MCP surface → `knowledge-service/docs/adr/`
- Touches only the Web UI → `knowledge-web/docs/adr/` (create when the first one appears)
- Changes the contract *between* the two, or the deployment topology as a whole → root `docs/adr/`

When in doubt, prefer the narrower context. A decision can be superseded by a wider-scoped one later; that is cheaper than a root ADR nobody reads.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

One known trap in this repo, pending a glossary: the Web UI calls the concept **「目录」**, while the backend, the database and the REST/MCP contract all still call it **`source`** (see `knowledge-service/docs/adr/`, and the 08-06 rename in `QA/`). Use `source` for backend-facing output and 「目录」 for user-facing output; do not "fix" one to match the other.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_

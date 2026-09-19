"""Operator CLI."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer

from .config import get_settings

# Windows consoles default to a legacy codepage; CJK corpora need UTF-8 output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(help="Wenmai Search - self-hosted knowledge retrieval", no_args_is_help=True)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@app.command("init")
def init_command() -> None:
    """Create the schema, the default tenant and an empty lexical index."""
    from .db.session import init_db
    from .lexical import get_lexical_store, reset_lexical_store

    _setup_logging(False)
    init_db()
    get_lexical_store()
    reset_lexical_store()
    settings = get_settings()
    typer.echo(
        f"initialised profile={settings.profile} db={settings.resolved_database_url} "
        f"vector={settings.vector_backend} lexical={settings.lexical_backend}"
    )


@app.command("add-source")
def add_source_command(
    name: str,
    kind: str = "filesystem",
    uri: str = "",
) -> None:
    """Register a source and print its id."""
    from .db import repo
    from .db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        source = repo.upsert_source(
            session, tenant_id=get_settings().default_tenant, name=name, kind=kind, uri=uri
        )
        typer.echo(source.id)


@app.command("issue-key")
def issue_key_command(name: str, acl: str = "public") -> None:
    """Create an API key. The plaintext is shown once."""
    from .api.auth import issue_key
    from .db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        raw, _ = issue_key(
            session,
            tenant_id=get_settings().default_tenant,
            name=name,
            acl=[tag.strip() for tag in acl.split(",") if tag.strip()],
        )
    typer.echo(raw)


@app.command("ingest")
def ingest_command(
    path: Path,
    source: str = typer.Option(..., help="source name (created if missing)"),
    patterns: str = typer.Option("*", help="comma separated glob patterns"),
    recursive: bool = True,
    limit: int = 100_000,
    run_worker: bool = typer.Option(True, help="drain the queue after registering"),
    verbose: bool = False,
) -> None:
    """Register files from a local path and (by default) index them immediately."""
    from .db import repo
    from .db.session import init_db, session_scope
    from .ingest.uploader import register_path

    _setup_logging(verbose)
    init_db()
    tenant = get_settings().default_tenant
    root = path.expanduser().resolve()

    with session_scope() as session:
        source_row = repo.upsert_source(
            session, tenant_id=tenant, name=source, kind="filesystem", uri=str(root)
        )
        source_id = source_row.id

    globs = [p.strip() for p in patterns.split(",") if p.strip()] or ["*"]
    files: list[Path] = []
    if root.is_file():
        files = [root]
    else:
        for pattern in globs:
            globber = root.rglob if recursive else root.glob
            files.extend(p for p in globber(pattern) if p.is_file())
    files = sorted(set(files))[:limit]

    registered = deduplicated = failed = 0
    for file_path in files:
        external_id = file_path.relative_to(root).as_posix() if root.is_dir() else file_path.name
        try:
            with session_scope() as session:
                result = register_path(
                    session,
                    tenant_id=tenant,
                    source_id=source_id,
                    path=file_path,
                    external_id=external_id,
                )
            if result.deduplicated:
                deduplicated += 1
            else:
                registered += 1
        except Exception as exc:
            failed += 1
            typer.echo(f"  ! {external_id}: {exc}", err=True)

    typer.echo(
        f"registered={registered} deduplicated={deduplicated} failed={failed} "
        f"source={source} ({source_id})"
    )

    if run_worker:
        from .ingest.worker import IngestWorker

        processed = IngestWorker().drain()
        typer.echo(f"worker processed {processed} job(s)")


@app.command("worker")
def worker_command(
    once: bool = typer.Option(False, help="drain the queue and exit"),
    max_jobs: int | None = None,
    verbose: bool = False,
) -> None:
    """Run the ingest worker."""
    from .db.session import init_db
    from .ingest.worker import IngestWorker

    _setup_logging(verbose)
    init_db()
    worker = IngestWorker()
    processed = worker.drain() if once else worker.run_forever(max_jobs=max_jobs)
    typer.echo(f"processed {processed} job(s)")


@app.command("reembed")
def reembed_command(
    batch_size: int = typer.Option(128, help="chunks per embedding batch"),
    keep_collection: bool = typer.Option(
        False, help="skip recreating the collection (only valid if the dim is unchanged)"
    ),
    resume_after: str = typer.Option(
        "", help="continue an interrupted run after this chunk id (implies --keep-collection)"
    ),
    verbose: bool = False,
) -> None:
    """Rebuild every vector from the stored chunks - run this after changing the model.

    Parsing and chunking are not repeated: chunk ids, offsets and heading paths
    do not depend on the embedding model.
    """
    from .db.session import init_db
    from .ingest.reembed import reembed_tenant

    _setup_logging(verbose)
    init_db()
    settings = get_settings()

    state = {"last": -1}

    def on_progress(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 100
        if percent != state["last"]:
            state["last"] = percent
            # One line per percent, not a \r-updated line: this run takes long
            # enough that the log is the only record if the process is killed.
            typer.echo(f"  {done}/{total} chunks ({percent}%)")

    typer.echo(f"provider={settings.dense_provider} model={settings.dense_model}")
    result = reembed_tenant(
        settings.default_tenant,
        batch_size=batch_size,
        recreate=not keep_collection,
        resume_after=resume_after or None,
        progress=on_progress,
    )
    typer.echo(
        f"re-embedded {result.chunks} chunks across {result.documents} documents "
        f"(model={result.model}, dim={result.dim})"
    )
    typer.echo(f"last_chunk_id={result.last_chunk_id}")


@app.command("rebuild-lexical")
def rebuild_lexical_command(
    batch_size: int = typer.Option(512, help="chunks per index batch"),
    verbose: bool = False,
) -> None:
    """Rebuild the lexical index from the stored chunks.

    Repair path if the lexical side disagrees with `chunks`. Dense vectors are
    untouched, so this is cheaper than a full `reembed`.
    """
    from .db.session import init_db
    from .ingest.reembed import rebuild_lexical

    _setup_logging(verbose)
    init_db()
    settings = get_settings()

    state = {"last": -1}

    def on_progress(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 100
        if percent != state["last"]:
            state["last"] = percent
            typer.echo(f"  {done}/{total} chunks ({percent}%)")

    count = rebuild_lexical(
        settings.default_tenant, batch_size=batch_size, progress=on_progress
    )
    typer.echo(f"indexed {count} chunks into {settings.lexical_backend}")


plagiarism_app = typer.Typer(
    help="Plagiarism corpus lifecycle (PostgreSQL only, default-off)", no_args_is_help=True
)
app.add_typer(plagiarism_app, name="plagiarism")


def _plagiarism_preflight():
    """Fail fast, and never hint that SQLite might work.

    Also ensures the knowledge-base schema exists: every plagiarism command
    reads the document table, and `status` on a database that has never been
    initialised should say so rather than surface a raw SQL error.
    """
    from .db.session import get_engine, init_db
    from .plagiarism.schema import ensure_postgres

    engine = get_engine()
    ensure_postgres(engine)
    init_db()
    return engine


@plagiarism_app.command("init")
def plagiarism_init_command(verbose: bool = False) -> None:
    """Create the plag_* tables and indexes. Idempotent."""
    from .plagiarism.schema import init_plagiarism_schema

    _setup_logging(verbose)
    engine = _plagiarism_preflight()
    created = init_plagiarism_schema(engine)
    typer.echo(f"created {len(created)} table(s)" + (f": {', '.join(created)}" if created else ""))


@plagiarism_app.command("backfill")
def plagiarism_backfill_command(
    rebuild: bool = typer.Option(False, "--rebuild", help="re-register documents already built"),
    verbose: bool = False,
) -> None:
    """Register a projection build for every current document version.

    Only registers work; the plagiarism worker does the computing. Safe to
    interrupt and safe to re-run - registration is idempotent.
    """
    from .plagiarism.corpus_ops import register_backfill

    _setup_logging(verbose)
    _plagiarism_preflight()
    settings = get_settings()
    registered, skipped = register_backfill(settings.default_tenant, rebuild=rebuild)
    typer.echo(f"registered {registered} job(s), skipped {skipped} already built")


@plagiarism_app.command("rebuild")
def plagiarism_rebuild_command(verbose: bool = False) -> None:
    """Re-register every document, e.g. after an algorithm parameter change."""
    from .plagiarism.corpus_ops import register_backfill

    _setup_logging(verbose)
    _plagiarism_preflight()
    settings = get_settings()
    registered, _ = register_backfill(settings.default_tenant, rebuild=True)
    typer.echo(f"registered {registered} job(s) under config hash "
               f"{settings.plagiarism_algorithm_config_hash}")


@plagiarism_app.command("status")
def plagiarism_status_command() -> None:
    """Schema, worker liveness, corpus coverage and the current config hash."""
    from .plagiarism.corpus_ops import corpus_report

    _plagiarism_preflight()
    settings = get_settings()
    report = corpus_report(settings.default_tenant)
    typer.echo(f"config hash     {report['algorithm_config_hash']}")
    typer.echo(f"schema ready    {report['schema']['ready']}")
    if report["schema"]["missing_tables"]:
        typer.echo(f"  missing       {', '.join(report['schema']['missing_tables'])}")
    if report["schema"].get("missing_columns"):
        typer.echo(f"  missing cols  {', '.join(report['schema']['missing_columns'])}")
    typer.echo(f"  gin index     {report['schema']['gin_index']}")
    typer.echo(f"live workers    {report['live_workers']}")
    coverage = report["coverage"]
    typer.echo(
        f"corpus          {coverage['ready']}/{coverage['total']} ready, "
        f"{coverage['pending']} pending, {coverage['failed']} failed"
    )
    typer.echo(f"pending jobs    {report['pending_jobs']}")


@plagiarism_app.command("rebuild-df")
def plagiarism_rebuild_df_command(verbose: bool = False) -> None:
    """Recompute fingerprint document frequencies and ANALYZE the corpus."""
    from .plagiarism.corpus_ops import rebuild_document_frequencies

    _setup_logging(verbose)
    _plagiarism_preflight()
    settings = get_settings()
    count = rebuild_document_frequencies(settings.default_tenant)
    typer.echo(f"recomputed {count} fingerprint frequencies")


@plagiarism_app.command("cleanup")
def plagiarism_cleanup_command(verbose: bool = False) -> None:
    """Delete aged-out checks and retired projections."""
    from .plagiarism.corpus_ops import run_cleanup

    _setup_logging(verbose)
    _plagiarism_preflight()
    removed = run_cleanup()
    typer.echo(f"removed {removed['checks']} check(s), {removed['projections']} projection(s)")


@app.command("plagiarism-worker")
def plagiarism_worker_command(
    once: bool = typer.Option(False, "--once", help="drain the queues and exit"),
    max_jobs: int = typer.Option(1000, help="job ceiling for --once"),
    verbose: bool = False,
) -> None:
    """Run the plagiarism worker.

    A separate process from the ingest worker on purpose: alignment is CPU-bound
    and would otherwise stall parsing, embedding and index writes.
    """
    from .plagiarism.worker import PlagiarismWorker

    _setup_logging(verbose)
    _plagiarism_preflight()
    worker = PlagiarismWorker()
    if once:
        typer.echo(f"processed {worker.drain(max_jobs=max_jobs)} job(s)")
        return
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()
        typer.echo("stopped")


@app.command("backfill-analyzed")
def backfill_analyzed_command(
    batch_size: int = typer.Option(1000, help="chunks per update batch"),
    force: bool = typer.Option(False, help="recompute rows that already have a value"),
    verbose: bool = False,
) -> None:
    """Fill in `chunk.analyzed` for chunks ingested before the column existed.

    The lexical reranker reads it instead of re-tokenizing every candidate on
    every query. Until a chunk is backfilled it still reranks correctly, just
    at the old cost - so this is safe to run late, and safe to interrupt.

    Use `--force` after changing the tokenizer; the stored values are only
    valid for the analyzer that produced them.
    """
    from .db.session import init_db
    from .ingest.reembed import backfill_analyzed

    _setup_logging(verbose)
    init_db()
    settings = get_settings()

    state = {"last": -1}

    def on_progress(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 100
        if percent != state["last"]:
            state["last"] = percent
            typer.echo(f"  {done}/{total} chunks ({percent}%)")

    count = backfill_analyzed(
        settings.default_tenant, batch_size=batch_size, force=force, progress=on_progress
    )
    typer.echo(f"backfilled {count} chunks")


@app.command("search")
def search_command(
    query: str,
    top_k: int = 5,
    mode: str = "hybrid",
    debug: bool = False,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Query the index from the terminal."""
    from .db.session import init_db
    from .retrieval.pipeline import RetrievalRequest, get_retrieval_service

    init_db()
    response = get_retrieval_service().search(
        RetrievalRequest(
            query=query,
            tenant_id=get_settings().default_tenant,
            top_k=top_k,
            mode=mode,  # type: ignore[arg-type]
            debug=debug,
        )
    )
    payload = response.to_dict()
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not payload["results"]:
        typer.echo("no results")
        return
    for index, item in enumerate(payload["results"], start=1):
        breadcrumb = " › ".join(item["heading_path"]) or "(no heading)"
        typer.echo(f"\n[{index}] {item['title']} › {breadcrumb}")
        typer.echo(f"    score={item['score']} rerank={item['rerank_score']}")
        typer.echo(f"    {item['snippet']}")
        typer.echo(f"    cite: {item['document_id']}#{item['chunk_ordinal']}")
    if debug:
        typer.echo("\n--- debug ---")
        typer.echo(json.dumps(payload["debug"], ensure_ascii=False, indent=2))


@app.command("serve")
def serve_command(host: str = "", port: int = 0, reload: bool = False) -> None:
    """Run the REST API."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "kbsvc.api.app:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
    )


@app.command("mcp")
def mcp_command(
    transport: str = typer.Option("stdio", help="stdio | streamable-http | sse"),
    host: str = typer.Option("", help="bind address for the HTTP transports"),
    port: int = typer.Option(0, help="bind port for the HTTP transports"),
) -> None:
    """Run the MCP server."""
    from .mcp.server import run

    run(transport, host=host, port=port)


@app.command("stats")
def stats_command() -> None:
    """Print index counters."""
    from sqlalchemy import func, select

    from .db.models import Chunk, Document, DocumentVersion, IngestJob
    from .db.session import init_db, session_scope
    from .lexical import get_lexical_store
    from .vector import get_vector_store

    init_db()
    tenant = get_settings().default_tenant
    with session_scope() as session:

        def count(model, *conditions) -> int:
            return int(
                session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0
            )

        jobs = dict(
            session.execute(
                select(IngestJob.state, func.count(IngestJob.id))
                .where(IngestJob.tenant_id == tenant)
                .group_by(IngestJob.state)
            ).all()
        )
        payload = {
            "documents": count(
                Document, Document.tenant_id == tenant, Document.deleted_at.is_(None)
            ),
            "versions": count(DocumentVersion, DocumentVersion.tenant_id == tenant),
            "chunks": count(Chunk, Chunk.tenant_id == tenant),
            "jobs_by_state": jobs,
        }
    try:
        payload["vector_points"] = get_vector_store().count(tenant)
    except Exception as exc:
        payload["vector_points"] = f"error: {exc}"
    try:
        payload["lexical_docs"] = get_lexical_store().count(tenant)
    except Exception as exc:
        payload["lexical_docs"] = f"error: {exc}"
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()

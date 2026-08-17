"""Transactional publish (ADR-0008 ticket 08).

The whole point of tickets 03/04/06/07 - writing vector, lexical and metadata
inside one database transaction - is only real once the *caller* passes one
session into every store. These tests exercise the worker's publish path, both
delete paths, and reembed's per-batch commit against the in-database backends
(sqlite-vec + fts5, which both enlist in the caller's session), and assert that
a crash mid-publish rolls the whole version back: no dense-only or lexical-only
chunks, no orphan chunk rows.

The server-profile dual-writer throughput criterion (acceptance #4) needs a
reachable ParadeDB plus two real worker processes, so it is not exercised here -
it is PG-gated the same way test_pgvector_store.py / test_pg_search_store.py
are, and is documented in the issue's Comments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kbsvc.config import reset_settings_cache
from kbsvc.db.session import init_db, reset_engine_cache, session_scope
from kbsvc.embedding import reset_embedder_cache
from kbsvc.lexical import reset_lexical_store
from kbsvc.lexical.fts5_store import Fts5LexicalStore
from kbsvc.storage import reset_object_store_cache
from kbsvc.vector import reset_vector_store
from kbsvc.vector.sqlite_vec_store import SqliteVecStore

DOC = (
    "# 六壬\n\n## 卷一\n\n"
    "贼克者，取用之首法也。上克下为贼，下贼上为克。\n"
    "凡四课之中，有一下贼上者，即取之为用神。\n\n"
    "## 卷二\n\n涉害者，比用不成则涉害。涉害深者为用。\n"
)


def _setup_consolidated_env(monkeypatch, tmp_path: Path) -> None:
    """Point everything at a fresh single-file SQLite DB using the in-database
    stores (sqlite-vec + fts5), the only backends that enlist in a caller
    session. init_db builds the metadata + FTS5 schema; the vec0 table is
    created lazily by ensure_collection on the first ingest."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("KB_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KB_PROFILE", "local")
    monkeypatch.setenv("KB_VECTOR_BACKEND", "sqlite-vec")
    monkeypatch.setenv("KB_LEXICAL_BACKEND", "fts5")
    monkeypatch.setenv("KB_DEFAULT_TENANT", "test")
    monkeypatch.setenv("KB_DENSE_PROVIDER", "hash")
    monkeypatch.setenv("KB_DENSE_DIM", "128")
    # Fail a job on the first attempt so a crash injection parks it failed
    # immediately rather than retrying and re-hitting the patched method.
    monkeypatch.setenv("KB_WORKER_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("KB_API_WORKER_ENABLED", "false")

    reset_settings_cache()
    reset_engine_cache()
    reset_object_store_cache()
    reset_embedder_cache()
    reset_vector_store()
    reset_lexical_store()
    init_db()


@pytest.fixture
def consolidated_env(monkeypatch, tmp_path):
    _setup_consolidated_env(monkeypatch, tmp_path)
    yield
    # Tear down the per-test engine/stores before tmp_path is removed, then
    # undo the env and reset caches so later test files see the session env
    # again - mirrors test_sqlite_vec_store / test_fts5_lexical_store's
    # _restore_global_environment.
    reset_vector_store()
    reset_lexical_store()
    monkeypatch.undo()
    reset_settings_cache()
    reset_engine_cache()
    reset_object_store_cache()
    reset_embedder_cache()


def _register_and_drain(text: str = DOC, *, external_id: str = "atomic/a.md"):
    """Register one document and run the worker. Returns (result, worker)."""
    from kbsvc.db import repo
    from kbsvc.ingest.uploader import register_bytes
    from kbsvc.ingest.worker import IngestWorker

    worker = IngestWorker()
    with session_scope() as session:
        tenant_id = "test"
        source = repo.upsert_source(
            session, tenant_id=tenant_id, name="pytest", kind="upload"
        )
        result = register_bytes(
            session,
            tenant_id=tenant_id,
            source_id=source.id,
            external_id=external_id,
            data=text.encode(),
            filename="a.md",
        )
    worker.drain()
    return result, worker


def _indexed_counts(tenant_id: str = "test") -> tuple[int, int, int]:
    """(chunk rows, dense vectors, lexical docs) for a tenant."""
    from kbsvc.db.models import Chunk
    from kbsvc.lexical import get_lexical_store
    from kbsvc.vector import get_vector_store

    with session_scope() as session:
        chunks = (
            session.query(Chunk).filter_by(tenant_id=tenant_id).count()
        )
    return chunks, get_vector_store().count(tenant_id), get_lexical_store().count(tenant_id)


# --- happy path sanity ---------------------------------------------------


def test_publish_commits_metadata_dense_and_lexical_together(consolidated_env):
    """The in-database backends work end to end: after a successful ingest all
    three stores agree on the chunk count. This is the control against which
    the crash tests below compare."""
    result, _ = _register_and_drain()

    chunks, dense, lexical = _indexed_counts()
    assert chunks > 0
    assert dense == chunks
    assert lexical == chunks

    with session_scope() as session:
        from kbsvc.db.models import DocumentVersion

        version = session.get(DocumentVersion, result.version_id)
        assert version.status == "indexed"


# --- crash: publish path -------------------------------------------------


def test_crash_after_dense_write_before_lexical_rolls_back_the_whole_version(
    consolidated_env, monkeypatch
):
    """Simulate the exact split the issue describes: the vector upsert has
    executed inside the publish transaction, then the process dies before the
    lexical write. With ticket 08's shared session, everything rolls back -
    no dense-only residue, no orphan chunk rows, no half-indexed version."""
    from kbsvc.db.models import Chunk, DocumentVersion, IngestJob
    from kbsvc.ingest.states import JobState

    # Make the very first lexical write in the publish block raise. The vector
    # upsert runs immediately before it on the same session, so this is the
    # "dense written, lexical not yet" crash point.
    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated crash after dense write")

    monkeypatch.setattr(Fts5LexicalStore, "delete_by_versions", _boom)

    result, _ = _register_and_drain()

    # The job failed and the version never reached indexed.
    with session_scope() as session:
        job = session.get(IngestJob, result.job_id)
        version = session.get(DocumentVersion, result.version_id)
        assert job.state == str(JobState.FAILED)
        assert version.status == "failed"

        # No chunk rows survived the rollback...
        assert (
            session.query(Chunk).filter_by(version_id=result.version_id).count() == 0
        )

    # ...and neither did any vector or lexical document. The three counts must
    # agree at zero - the whole point of the ticket.
    chunks, dense, lexical = _indexed_counts()
    assert chunks == 0
    assert dense == 0
    assert lexical == 0


def test_crash_after_lexical_write_rolls_back_too(consolidated_env, monkeypatch):
    """The mirror case: the lexical upsert ran, then a later step (version
    finalisation) fails. The lexical write must still roll back with the
    session - lexical-only residue is just as wrong as dense-only."""
    from kbsvc.db.models import Chunk, IngestJob
    from kbsvc.ingest import worker as worker_mod
    from kbsvc.ingest.states import JobState

    # Patch _finish (called after both store writes, when the job is being
    # marked COMPLETED) to raise. Both index writes have already executed on
    # the session by this point, so this is the "lexical written, then crash
    # before commit" point.
    def _finish_boom(self, session, job, state):
        raise RuntimeError("simulated crash after both writes")

    monkeypatch.setattr(worker_mod.IngestWorker, "_finish", _finish_boom)

    result, _ = _register_and_drain()

    with session_scope() as session:
        job = session.get(IngestJob, result.job_id)
        assert job.state == str(JobState.FAILED)
        assert (
            session.query(Chunk).filter_by(version_id=result.version_id).count() == 0
        )

    chunks, dense, lexical = _indexed_counts()
    assert chunks == dense == lexical == 0


# --- crash: delete path --------------------------------------------------


def test_crash_during_document_delete_rolls_back_both_indexes(
    consolidated_env, monkeypatch
):
    """The document-delete path removes metadata chunks and both index entries
    in one transaction. A crash between the two index deletes must not leave a
    chunk searchable in one index after its metadata row is gone."""
    from kbsvc.db.models import Chunk, Document, IngestJob
    from kbsvc.ingest.states import JobState, JobType

    # First get a fully indexed document.
    result, _ = _register_and_drain()
    chunks_before, dense_before, lexical_before = _indexed_counts()
    assert chunks_before > 0

    # Now enqueue a delete and make the *lexical* half of the paired delete
    # raise (vector delete runs first in _run_delete).
    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated crash during delete")

    monkeypatch.setattr(Fts5LexicalStore, "delete_by_document", _boom)

    from kbsvc.ingest.worker import IngestWorker

    with session_scope() as session:
        document = session.get(Document, result.document_id)
        job = IngestJob(
            id="job-delete-crash",
            tenant_id="test",
            document_id=document.id,
            job_type=JobType.DELETE,
            state=str(JobState.PENDING),
            attempts=0,
            max_attempts=1,
        )
        session.add(job)
    IngestWorker().drain()

    with session_scope() as session:
        job = session.get(IngestJob, "job-delete-crash")
        assert job.state == str(JobState.FAILED)
        # The document must NOT be marked deleted - the metadata delete rolled
        # back with the index writes.
        document = session.get(Document, result.document_id)
        assert document.deleted_at is None
        # And the chunk rows are still present.
        assert (
            session.query(Chunk).filter_by(document_id=result.document_id).count()
            == chunks_before
        )

    # Crucially the dense delete rolled back too - both indexes still hold the
    # document, matching the metadata. No half-deleted state.
    chunks, dense, lexical = _indexed_counts()
    assert chunks == chunks_before
    assert dense == dense_before
    assert lexical == lexical_before


def test_crash_during_supersede_rolls_back_both_indexes(
    consolidated_env, monkeypatch
):
    """The supersede path (an older version retired when a new one publishes)
    also pairs vector + lexical deletes. A crash there must roll both back."""
    from kbsvc.db.models import Chunk

    # Index v1.
    v1, _ = _register_and_drain(external_id="super/a.md")
    # Index v2 (same external id -> new version, supersedes v1). This should
    # succeed and retire v1's chunks.
    v2, _ = _register_and_drain(DOC + "\n附录补遗一段文字也。\n", external_id="super/a.md")

    with session_scope() as session:
        v1_chunks = session.query(Chunk).filter_by(version_id=v1.version_id).count()
        v2_chunks = session.query(Chunk).filter_by(version_id=v2.version_id).count()
    assert v1_chunks == 0  # superseded
    assert v2_chunks > 0

    # Now force the supersede delete to fail on a third publish. The crash must
    # not leave v2's chunks deleted from one index but present in the other.
    def _boom(self, *args, **kwargs):
        raise RuntimeError("simulated crash during supersede")

    monkeypatch.setattr(SqliteVecStore, "delete_by_versions", _boom)

    v3, _ = _register_and_drain(
        DOC + "\n第三版增补又一段不同的文字。\n", external_id="super/a.md"
    )

    with session_scope() as session:
        # v3 did not publish.
        assert (
            session.query(Chunk).filter_by(version_id=v3.version_id).count() == 0
        )
        # v2 is untouched - its supersede ran in the same transaction that
        # just rolled back, so v2's chunks are all still there in both
        # indexes.
        v2_after = session.query(Chunk).filter_by(version_id=v2.version_id).count()
    assert v2_after == v2_chunks

    # All three stores agree: exactly v2's chunks, nothing more, nothing less.
    chunks, dense, lexical = _indexed_counts()
    assert chunks == v2_chunks
    assert dense == v2_chunks
    assert lexical == v2_chunks


# --- reembed: per-batch transaction -------------------------------------


def test_reembed_batch_failure_leaves_no_partial_batch(consolidated_env, monkeypatch):
    """reembed commits one transaction per batch (ticket 08), and a crash
    inside a batch rolls that whole batch back in both indexes - no chunk
    left dense-only or lexical-only."""
    from kbsvc.ingest.reembed import reembed_tenant

    # Index a document so there is something to re-embed.
    _register_and_drain()
    chunks_before, dense_before, lexical_before = _indexed_counts()
    assert chunks_before >= 2  # the doc produces multiple chunks

    # Force the dense upsert of the second batch to raise. With a batch size
    # of 1, each chunk is its own batch; the first batch commits, the second
    # rolls back entirely.
    original_upsert = SqliteVecStore.upsert
    calls = {"n": 0}

    def _flaky(self, points, *, session=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-reembed")
        return original_upsert(self, points, session=session)

    monkeypatch.setattr(SqliteVecStore, "upsert", _flaky)

    with pytest.raises(RuntimeError, match="mid-reembed"):
        reembed_tenant("test", batch_size=1)

    # Exactly one chunk's batch committed (in both indexes); the second batch
    # rolled back before the lexical write, so the two indexes never disagree -
    # no dense-only / lexical-only residue. Metadata is untouched by reembed.
    chunks, dense, lexical = _indexed_counts()
    assert chunks == chunks_before
    assert dense == 1
    assert lexical == 1


def test_reembed_resume_after_uses_per_batch_checkpoint(consolidated_env):
    """`--resume-after` still works after the per-batch transaction change:
    the checkpoint is a committed chunk id, and a resumed run reaches the
    same final state as a clean run."""
    from kbsvc.ingest.reembed import reembed_tenant

    _register_and_drain()
    chunks_before, _, _ = _indexed_counts()
    assert chunks_before >= 2

    # First run: process a couple of batches, capture the checkpoint.
    first = reembed_tenant("test", batch_size=2)
    assert first.chunks == chunks_before

    # Resume from the reported checkpoint must be a no-op (everything at or
    # below last_chunk_id is already written) and leave both indexes whole.
    resumed = reembed_tenant(
        "test", batch_size=2, recreate=False, resume_after=first.last_chunk_id
    )
    assert resumed.chunks == 0

    chunks, dense, lexical = _indexed_counts()
    assert chunks == chunks_before
    assert dense == chunks
    assert lexical == chunks


# --- server profile: concurrent writers (PG-gated) -----------------------


def test_two_workers_concurrent_publish_is_consistent(monkeypatch, tmp_path):
    """Acceptance #4 (the core promise of the whole ADR): with pgvector +
    pg_search sharing one PostgreSQL database, two workers publishing disjoint
    documents at the same time must not hit lock errors, and when both commit
    the metadata, dense and lexical halves all agree.

    Two threads each driving their own session is the deterministic in-process
    proxy for two worker processes - Postgres MVCC does not distinguish the
    two, and each thread goes through the exact
    ``session_scope -> ensure -> upsert/delete -> commit`` publish block the
    worker uses. Skips cleanly without a reachable KB_TEST_POSTGRES_URL (the
    local profile is single-process by construction - acceptance #4 exempts
    it). The throughput-numbers-with-N-workers part of acceptance #4 is a
    deployment benchmark recorded separately in docs/05-performance.md, not a
    unit assertion.
    """
    import os
    import threading
    import uuid

    from sqlalchemy import create_engine, text

    url = os.environ.get(
        "KB_TEST_POSTGRES_URL",
        "postgresql+psycopg://kbsvc:kbsvc@postgres:5432/kbsvc_test",
    )
    engine = create_engine(url, pool_pre_ping=True, future=True, pool_size=10)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - any driver/network failure skips
        engine.dispose()
        pytest.skip(f"no PostgreSQL reachable at {url}: {type(exc).__name__}")

    # Point the app's globals at the live Postgres for this one test.
    monkeypatch.setenv("KB_DATABASE_URL", url)
    monkeypatch.setenv("KB_PROFILE", "server")
    monkeypatch.setenv("KB_VECTOR_BACKEND", "pgvector")
    monkeypatch.setenv("KB_LEXICAL_BACKEND", "pg-search")
    monkeypatch.setenv("KB_DENSE_PROVIDER", "hash")
    monkeypatch.setenv("KB_DENSE_DIM", "128")
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))

    from kbsvc.config import reset_settings_cache
    from kbsvc.db.models import Base, Chunk
    from kbsvc.db.session import reset_engine_cache, session_scope
    from kbsvc.embedding import reset_embedder_cache
    from kbsvc.lexical import LexicalDocument, reset_lexical_store
    from kbsvc.vector import VectorPoint, reset_vector_store

    reset_settings_cache()
    reset_engine_cache()
    reset_embedder_cache()
    reset_vector_store()
    reset_lexical_store()

    from kbsvc.db.session import ensure_postgres_extensions

    try:
        ensure_postgres_extensions(engine)
        Base.metadata.create_all(engine)

        from kbsvc.lexical import get_lexical_store
        from kbsvc.vector import get_vector_store

        # `recreate_collection`, not `ensure_pgvector_schema` directly: the
        # latter's `ADD COLUMN IF NOT EXISTS` never touches an existing
        # column's type, so a dimension left behind by another PG-gated test
        # file sharing this database (e.g. test_pgvector_store.py's
        # `FAST_DIM=16`, by design local to that module - see its
        # `_fast_schema` fixture's own docstring) would silently survive and
        # make every `ensure_collection(128, ...)` below raise
        # `KbError("... different embedding dimension")` instead of the
        # concurrency behaviour this test exists to exercise. Mirrors
        # `test_pgvector_store.py::_fast_schema`'s same defensive pattern in
        # the other direction. Doing this through the process-wide
        # `get_vector_store()`/`get_lexical_store()` singletons (rather than
        # a throwaway `PgVectorStore()`) also means both worker threads below
        # see an already-`_dim`-set store on their very first call, closing
        # the race where two brand-new threads could otherwise both race
        # into the schema-ensure DDL on their first chunk simultaneously.
        get_vector_store().recreate_collection(128)
        get_lexical_store().ensure_ready()

        tenant_id = f"txn-{uuid.uuid4().hex[:12]}"
        per_worker = 25
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def _publish(worker_index: int) -> None:
            try:
                from kbsvc.lexical.tokenizer import analyze as _analyze

                store = get_vector_store()
                lexical = get_lexical_store()
                barrier.wait()
                for i in range(per_worker):
                    chunk_id = f"{tenant_id}-w{worker_index}-{i}"
                    version_id = f"v-{chunk_id}"
                    body = f"worker {worker_index} chunk {i} 古籍文字内容"
                    dense = [float((i + 1) * (worker_index + 1 + j)) % 1.0 for j in range(128)]
                    payload = {
                        "tenant_id": tenant_id,
                        "document_id": f"doc-{worker_index}",
                        "version_id": version_id,
                        "source_id": "s1",
                        "kind": "section",
                        "acl": ["public"],
                        "is_current": True,
                        "text": body,
                    }
                    with session_scope() as session:
                        session.add(
                            Chunk(
                                id=chunk_id,
                                tenant_id=tenant_id,
                                document_id=f"doc-{worker_index}",
                                version_id=version_id,
                                ordinal=i,
                                kind="section",
                                text=body,
                                token_count=len(body),
                                char_start=0,
                                char_end=len(body),
                                section_id="",
                                heading_path=[],
                                analyzed=_analyze(body),
                                content_hash=f"h-{chunk_id}",
                            )
                        )
                        # The real worker path flushes inside
                        # `repo.replace_chunks` before ever touching a store's
                        # UPDATE-shaped upsert (see db/repo.py). This hand-
                        # rolled publish loop must do the same explicitly, or
                        # the chunk row is still only pending when the stores'
                        # UPDATE statements run below - they match zero rows
                        # (silent no-op, per their UPDATE-not-INSERT contract)
                        # and only `session_scope`'s final commit actually
                        # creates the row, too late for this iteration's
                        # dense/lexical writes.
                        session.flush()
                        store.ensure_collection(128, session=session)
                        lexical.ensure_ready(session=session)
                        store.upsert([VectorPoint(id=chunk_id, dense=dense, payload=payload)],
                                     session=session)
                        lexical.upsert([LexicalDocument(id=chunk_id, text=body, payload=payload)],
                                       session=session)
            except Exception as exc:  # noqa: BLE001 - surfaced to the main thread
                errors.append(exc)

        threads = [threading.Thread(target=_publish, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"concurrent publish raised: {errors!r}"
        assert all(not t.is_alive() for t in threads), "a worker thread hung"

        with session_scope() as session:
            chunk_rows = (
                session.query(Chunk).filter_by(tenant_id=tenant_id).count()
            )
        dense_count = get_vector_store().count(tenant_id)
        lexical_count = get_lexical_store().count(tenant_id)
        assert chunk_rows == per_worker * 2
        assert dense_count == chunk_rows, "dense index missing chunks"
        assert lexical_count == chunk_rows, "lexical index missing chunks"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": tenant_id})
        engine.dispose()
        reset_vector_store()
        reset_lexical_store()
        monkeypatch.undo()
        reset_settings_cache()
        reset_engine_cache()
        reset_embedder_cache()

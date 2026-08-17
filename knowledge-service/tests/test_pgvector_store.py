"""Tests for PgVectorStore (ADR-0008 ticket 06).

Covers all 8 VectorStore Protocol methods plus the ticket's acceptance
criteria:
- ensure_collection/recreate_collection dimension handling, six required
  indexes present
- upsert is UPDATE-shaped: requires a pre-existing base `chunk` row (written
  by `db.repo.replace_chunks` before `store.upsert` runs in production) and
  silently no-ops when that row does not exist
- delete_by_* null the embedding/vector_payload columns and never delete the
  row itself
- search_dense pushes every SearchFilter field down, including acl as an
  array-overlap (`&&`) against a GIN index rather than a Python post-filter
  (SqliteVecStore's constraint, not this store's)
- sentinel: a narrow filter returns non-empty results at a scale large enough
  for Postgres's planner to actually prefer the btree/GIN index over a
  sequential scan, paired with an EXPLAIN-based proof it really does
- a *second*, distinct planner failure mode found via the ticket's own
  required middle-selectivity measurement (not in the ticket text): a
  multi-valued (`= ANY(...)`/`&&`) filter can make the default planner pick
  the same HNSW-ignores-the-filter plan even with the right btree/GIN index
  present, because its LIMIT-cost estimate assumes matches are spread evenly
  across the HNSW scan order - false whenever the filter correlates with the
  corpus's semantic clustering. `PgVectorStore.search_dense` now forces
  `SET LOCAL enable_indexscan = off` for exactly those filter shapes;
  `TestExactScanOverride` proves the override engages/doesn't leak, the 100k
  benchmark's mid-selectivity tier proves it fixes the real bug
- 100k-scale three-tier (0.5% / 10% / 90% selectivity) Recall@10 + query
  latency, ground truth computed via `SET LOCAL enable_indexscan = off`
  (exact brute force) - gated behind KB_RUN_SLOW_TESTS, same convention
  `tests/test_sqlite_vec_store.py`'s 100k sqlite-vec benchmark uses

Runs against a real PostgreSQL/ParadeDB instance - point KB_TEST_POSTGRES_URL
at one, or this whole module skips cleanly (mirrors
`tests/test_postgres_extensions.py` and `tests/plagiarism/conftest.py`).

Test isolation note: the `chunk` table is shared and persists across test
runs, unlike SQLite's per-test tmp file. `uq_chunk_position` is a UNIQUE
constraint on `(version_id, ordinal)` with no tenant_id in it - confirmed
live against the dev database - so every helper here mints a fresh globally
unique `version_id` per chunk rather than reusing short literals like `"v1"`
the way the SQLite-backed suites safely do.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from kbsvc.errors import KbError
from kbsvc.vector.base import SearchFilter, VectorPoint

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Default targets the compose stack from inside the compose network, same
# convention as tests/plagiarism/conftest.py and test_postgres_extensions.py.
# Override for a host-side run, e.g. postgresql+psycopg://kbsvc:...@127.0.0.1:55433/kbsvc
_DEFAULT_URL = "postgresql+psycopg://kbsvc:kbsvc@postgres:5432/kbsvc_test"

_SKIP_REASON = (
    "no PostgreSQL reachable at KB_TEST_POSTGRES_URL ({url}): {error}. "
    "ADR-0008 ticket 06's PgVectorStore assertions did NOT run."
)

# Shared dimension for every test except the 100k benchmark, which needs 512
# to produce numbers comparable to the ticket's own spike measurements. Small
# on purpose: dimension has no bearing on filter-pushdown or transactional
# correctness, and a small vector keeps this file's literals readable.
FAST_DIM = 16


def _target_url() -> str:
    return os.environ.get("KB_TEST_POSTGRES_URL", _DEFAULT_URL)


@pytest.fixture(scope="module")
def pg_engine():
    """Engine against the test database, or a loud skip. Mirrors
    `tests/test_postgres_extensions.py`'s `pg_engine` fixture exactly."""
    url = _target_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any driver/network failure skips
        engine.dispose()
        pytest.skip(_SKIP_REASON.format(url=url, error=type(exc).__name__), allow_module_level=True)

    from kbsvc.db.models import Base

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def _pg_env(pg_engine):
    """Point the app's global settings/engine at the live test Postgres for
    the whole module. Uses `pytest.MonkeyPatch()` directly rather than the
    function-scoped `monkeypatch` fixture, which pytest does not allow a
    module-scoped fixture to depend on - this is the documented pattern for
    exactly that scope mismatch."""
    mp = pytest.MonkeyPatch()
    # `str(engine.url)` masks the password (renders `***`) - the app's own
    # engine would then genuinely fail to authenticate. Mirrors
    # `tests/plagiarism/conftest.py`'s `app_db_on_test_postgres` fixture.
    mp.setenv("KB_DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    mp.setenv("KB_PROFILE", "server")
    mp.setenv("KB_VECTOR_BACKEND", "pgvector")
    from kbsvc.config import reset_settings_cache
    from kbsvc.db.session import reset_engine_cache

    reset_settings_cache()
    reset_engine_cache()
    yield
    mp.undo()
    reset_settings_cache()
    reset_engine_cache()


@pytest.fixture(scope="module")
def _fast_schema(pg_engine, _pg_env):
    """Force `chunk.embedding` to FAST_DIM once per module, regardless of
    whatever a previous session left behind (e.g. the 100k benchmark's
    dim=512, or a stale dimension from a different ticket's manual testing).
    `recreate_collection`, not `ensure_collection`: it must not raise on a
    pre-existing mismatched dimension, it must fix it."""
    from kbsvc.vector.pgvector_store import PgVectorStore

    store = PgVectorStore()
    store.recreate_collection(FAST_DIM)
    return store


@pytest.fixture
def store(_fast_schema, pg_engine):
    """A PgVectorStore pointed at the live test Postgres, with the shared
    schema already at FAST_DIM."""
    from kbsvc.vector.pgvector_store import PgVectorStore

    s = PgVectorStore()
    s.ensure_collection(FAST_DIM)  # cheap: schema already correct, just sets s._dim
    return s


@pytest.fixture
def tenant(pg_engine):
    """A fresh, globally-unique tenant_id per test, with its `chunk` rows
    cleaned up afterward. Needed because the dev database persists across
    test runs, unlike SQLite's per-test tmp file."""
    tid = f"pgvec-{uuid.uuid4().hex[:12]}"
    yield tid
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": tid})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_suffix() -> str:
    return uuid.uuid4().hex[:12]


def _vec(*values: float, dim: int = FAST_DIM) -> list[float]:
    """Pad/truncate to *dim*. `_vec(1.0, 0.0)` reads as "mostly along axis 0"
    the same way the sqlite-vec suite's bare `[1.0, 0.0, 0.0, 0.0]` literals
    do, without hardcoding FAST_DIM's exact width into every call site."""
    padded = list(values) + [0.0] * max(0, dim - len(values))
    return padded[:dim]


def _insert_base_chunk(
    pg_engine,
    *,
    chunk_id: str,
    tenant_id: str,
    document_id: str,
    version_id: str,
    ordinal: int = 0,
    kind: str = "section",
    text_: str = "hello",
) -> None:
    """Insert the base `chunk` row `store.upsert` expects to already exist -
    mirrors what `db.repo.replace_chunks` writes in production, before
    `ingest/worker.py` ever calls `store.upsert`."""
    from kbsvc.db.models import Chunk

    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        session.add(
            Chunk(
                id=chunk_id,
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                ordinal=ordinal,
                kind=kind,
                text=text_,
                token_count=1,
                char_start=0,
                char_end=len(text_),
                section_id="",
                heading_path=[],
                content_hash=f"hash-{chunk_id}",
            )
        )
        session.commit()
    finally:
        session.close()


def _seed_point(
    pg_engine,
    tenant_id: str,
    *,
    document_id: str = "d1",
    source_id: str = "s1",
    acl: list[str] | None = None,
    is_current: bool = True,
    kind: str = "section",
    text_: str = "hello",
    dense: list[float] | None = None,
    ordinal: int = 0,
    version_id: str | None = None,
) -> VectorPoint:
    """Insert a base `chunk` row and return the matching `VectorPoint` -
    the shape every write test calls `store.upsert` with, per the module
    docstring's UPDATE-not-INSERT contract."""
    suffix = _new_suffix()
    chunk_id = f"c-{suffix}"
    version_id = version_id or f"v-{suffix}"
    _insert_base_chunk(
        pg_engine,
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        ordinal=ordinal,
        kind=kind,
        text_=text_,
    )
    return VectorPoint(
        id=chunk_id,
        dense=dense if dense is not None else _vec(1.0),
        payload={
            "tenant_id": tenant_id,
            "document_id": document_id,
            "version_id": version_id,
            "source_id": source_id,
            "acl": acl if acl is not None else ["public"],
            "is_current": is_current,
            "kind": kind,
            "text": text_,
        },
    )


def _bulk_seed_clustered(
    pg_engine,
    tenant_id: str,
    *,
    n_rows: int,
    n_docs: int,
    dim: int,
    rare_acl_rows: set[int] | None = None,
) -> None:
    """Bulk-insert *n_rows* clustered chunks straight into the real `chunk`
    table for the EXPLAIN/sentinel tests, which need enough rows for
    Postgres's planner to actually prefer an index over a sequential scan -
    the small per-row `_seed_point`-based tests elsewhere in this file do not
    need this (filter pushdown correctness does not depend on scale).

    Adapts `.scratch/storage-consolidation/paradedb_fixture.sql`'s clustering
    technique (cluster center per "source", scaled so within-cluster cosine
    similarity is realistic rather than the near-orthogonal degeneracy plain
    random high-dimensional vectors produce) directly onto the real schema,
    respecting the fixture's documented data-generation traps: the cluster
    center subquery is correlated on `i` (`generate_series(1, dim + 0*k)`) so
    it evaluates per row instead of being cached as a single shared vector.
    """
    suffix = _new_suffix()
    ctr_table = f"sentinel_ctr_{suffix}"
    rare = rare_acl_rows or set()
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                f'CREATE TEMPORARY TABLE "{ctr_table}" AS '
                "SELECT k, (SELECT array_agg(random_normal())::real[] "
                "FROM generate_series(1, :dim + 0*k)) AS vec "
                "FROM generate_series(0, :n_docs - 1) k"
            ),
            {"dim": dim, "n_docs": n_docs},
        )
        conn.execute(
            text(
                "INSERT INTO chunk (id, tenant_id, document_id, version_id, ordinal, "
                "kind, text, token_count, char_start, char_end, section_id, "
                "heading_path, content_hash, analyzed, created_at, source_id, acl, "
                "is_current, embedding) "
                "SELECT 'sent-' || :suffix || '-' || i, :tenant, 'doc' || (i % :n_docs), "
                "'ver-sent-' || :suffix || '-' || i, 0, 'section', 't', 1, 0, 1, '', '[]', "
                "'h' || i, '', now(), 'src' || (i % :n_docs), "
                "CASE WHEN i = ANY(:rare_rows) THEN ARRAY['rare-tag'] ELSE ARRAY['public'] END, "
                "true, "
                "l2_normalize((SELECT array_agg(v * 1.53 + random_normal()) "
                f'FROM unnest((SELECT vec FROM "{ctr_table}" WHERE k = i % :n_docs)) '
                "AS u(v))::real[]::vector) "
                "FROM generate_series(1, :n_rows) i"
            ),
            {
                "suffix": suffix,
                "tenant": tenant_id,
                "n_docs": n_docs,
                "n_rows": n_rows,
                "rare_rows": list(rare),
            },
        )
        conn.execute(text("ANALYZE chunk"))


def _explain(pg_engine, sql: str, params: dict) -> str:
    with pg_engine.connect() as conn:
        rows = conn.execute(text(f"EXPLAIN {sql}"), params).fetchall()
    return "\n".join(row[0] for row in rows)


# ---------------------------------------------------------------------------
# 1. Regression: constructing PgVectorStore must never touch a non-Postgres
# engine, even transitively via the "connect" event listener it registers.
# ---------------------------------------------------------------------------


class TestVectorAdapterDialectGuard:
    """A real bug this ticket's own full-suite run surfaced (not caught by
    this file alone - it needs `tests/test_store_factories.py`'s pgvector
    construction tests in the mix): `PgVectorStore.__init__` used to attach
    the psycopg-specific `register_vector` "connect" listener to whatever
    `get_engine()` returned, unconditionally. `get_engine()` is a
    process-wide `lru_cache`d singleton, and `test_store_factories.py`'s
    `select_backend` fixture swaps `KB_VECTOR_BACKEND` without resetting the
    engine cache (a pre-existing gap in that fixture, not this store's to
    fix) - so constructing a `PgVectorStore` there can silently attach the
    listener to the *shared SQLite engine* every other test uses. The next
    real SQLite connection then crashed inside the listener with `TypeError:
    expected Connection or AsyncConnection, got Connection` (psycopg's
    `register_vector` handed a `sqlite3.Connection`). Fixed with a
    dialect gate in `_ensure_vector_adapter_registered`, the same convention
    `db/session.py::ensure_postgres_extensions` uses."""

    def test_sqlite_engine_is_never_touched(self, tmp_path):
        from sqlalchemy import create_engine

        from kbsvc.vector.pgvector_store import (
            _ensure_vector_adapter_registered,
            _vector_adapter_registered,
        )

        sqlite_engine = create_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
        try:
            _ensure_vector_adapter_registered(sqlite_engine)
            assert sqlite_engine not in _vector_adapter_registered

            # The actual failure mode: a connection must open and run a
            # query cleanly, not crash inside a wrongly-attached listener.
            with sqlite_engine.connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar() == 1
        finally:
            sqlite_engine.dispose()

    def test_constructing_the_store_against_a_sqlite_engine_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        """End-to-end companion: go through the real construction path
        (`get_engine()` pointed at SQLite, matching the stale-cache scenario
        that surfaced this bug) rather than calling the guarded helper
        directly."""
        monkeypatch.setenv("KB_DATABASE_URL", f"sqlite:///{(tmp_path / 't2.db').as_posix()}")
        monkeypatch.setenv("KB_PROFILE", "local")
        from kbsvc.config import reset_settings_cache
        from kbsvc.db.session import get_engine, reset_engine_cache

        reset_settings_cache()
        reset_engine_cache()
        try:
            from kbsvc.vector.pgvector_store import PgVectorStore

            PgVectorStore()  # must not raise

            with get_engine().connect() as conn:
                assert conn.execute(text("SELECT 1")).scalar() == 1
        finally:
            monkeypatch.undo()
            reset_settings_cache()
            reset_engine_cache()


# ---------------------------------------------------------------------------
# 2. ensure_collection / recreate_collection
# ---------------------------------------------------------------------------


class TestCollectionLifecycle:
    def test_ensure_collection_adds_columns_and_indexes(self, store, pg_engine):
        with pg_engine.connect() as conn:
            cols = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'chunk'"
                    )
                )
            }
            indexes = {
                row[0]
                for row in conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunk'")
                )
            }
        assert {"embedding", "source_id", "acl", "is_current", "vector_payload"} <= cols

        # Six btree/GIN indexes the ticket requires for every SearchFilter
        # field: tenant_id/document_id predate this ticket (declarative
        # `index=True` on Chunk), the other four are added by
        # ensure_pgvector_schema, plus the HNSW index itself.
        required_indexes = {
            "ix_chunk_tenant_id",
            "ix_chunk_document_id",
            "ix_chunk_source_id",
            "ix_chunk_acl_gin",
            "ix_chunk_kind",
            "ix_chunk_is_current",
            "ix_chunk_embedding_hnsw",
        }
        assert required_indexes <= indexes, f"missing: {required_indexes - indexes}"

    def test_ensure_collection_idempotent(self, store):
        store.ensure_collection(FAST_DIM)  # must not error

    def test_ensure_collection_dim_mismatch_raises(self, store):
        with pytest.raises(KbError, match="dimension"):
            store.ensure_collection(FAST_DIM + 1)

    def test_recreate_collection_clears_embeddings_but_keeps_the_row(
        self, store, pg_engine, tenant
    ):
        point = _seed_point(pg_engine, tenant)
        store.upsert([point])
        assert store.count(tenant) == 1

        store.recreate_collection(FAST_DIM)
        try:
            assert store.count(tenant) == 0

            with pg_engine.connect() as conn:
                row = conn.execute(
                    text("SELECT text, embedding FROM chunk WHERE id = :id"), {"id": point.id}
                ).first()
            assert row is not None, "recreate_collection must not delete the chunk row"
            assert row[0] == "hello"
            assert row[1] is None
        finally:
            # recreate_collection dropped and re-added the column globally;
            # restore FAST_DIM so later tests/fixtures in this module (and
            # this store instance's own cached _dim) are not left stranded.
            store.ensure_collection(FAST_DIM)


# ---------------------------------------------------------------------------
# 3. upsert + count
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_upsert_and_count(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, dense=_vec(0.0, 1.0))
        store.upsert([p1, p2])
        assert store.count(tenant) == 2

    def test_count_without_tenant_id_counts_every_tenant(self, store, pg_engine, tenant):
        """`chunk` is shared across tenants (and across this whole test
        module's other tests), so this only asserts the untenanted count is
        at least this test's own rows - not an exact total."""
        p1 = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, dense=_vec(0.0, 1.0))
        store.upsert([p1, p2])
        assert store.count() >= 2
        assert store.count() >= store.count(tenant)

    def test_upsert_updates_the_existing_row_rather_than_inserting(self, store, pg_engine, tenant):
        """upsert is UPDATE-shaped (module docstring): re-upserting the same
        chunk id must change its embedding/payload in place, not create a
        second row."""
        point = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0), text_="old")
        store.upsert([point])

        updated = VectorPoint(
            id=point.id,
            dense=_vec(0.0, 1.0),
            payload={**point.payload, "text": "new"},
        )
        store.upsert([updated])
        assert store.count(tenant) == 1

        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(_vec(0.0, 1.0), limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "new"

    def test_upsert_on_missing_base_row_is_a_silent_noop(self, store, tenant):
        """The base `chunk` row must already exist (module docstring) -
        `store.upsert` is never the thing that creates it. Calling it for an
        id with no matching row must not error and must not create a
        row - `ingest/worker.py::_persist_chunks` always runs first in
        production, so this only happens if the store is ever miscalled."""
        orphan = VectorPoint(
            id=f"missing-{_new_suffix()}",
            dense=_vec(1.0),
            payload={"tenant_id": tenant, "document_id": "d1", "text": "orphan"},
        )
        store.upsert([orphan])  # must not raise
        assert store.count(tenant) == 0

    def test_upsert_empty_list_is_noop(self, store, tenant):
        store.upsert([])
        assert store.count(tenant) == 0

    def test_upsert_before_ensure_collection_raises(self, pg_engine, tenant):
        from kbsvc.vector.pgvector_store import PgVectorStore

        fresh_store = PgVectorStore()  # never called ensure_collection
        point = _seed_point(pg_engine, tenant)
        with pytest.raises(KbError, match="ensure_collection"):
            fresh_store.upsert([point])


# ---------------------------------------------------------------------------
# 4. Deletes
# ---------------------------------------------------------------------------


class TestDeletes:
    def _setup_with_data(self, pg_engine, tenant):
        shared_version = f"v-{_new_suffix()}"
        p1 = _seed_point(
            pg_engine, tenant, document_id="d1", version_id=shared_version, ordinal=0,
            dense=_vec(1.0, 0.0),
        )
        p2 = _seed_point(
            pg_engine, tenant, document_id="d1", version_id=shared_version, ordinal=1,
            dense=_vec(0.0, 1.0),
        )
        p3 = _seed_point(pg_engine, tenant, document_id="d2", dense=_vec(0.5, 0.5))
        return p1, p2, p3, shared_version

    def test_delete_by_ids_nulls_embedding_but_keeps_the_row(self, store, pg_engine, tenant):
        p1, p2, p3, _ = self._setup_with_data(pg_engine, tenant)
        store.upsert([p1, p2, p3])

        store.delete_by_ids([p1.id])
        assert store.count(tenant) == 2

        with pg_engine.connect() as conn:
            row = conn.execute(
                text("SELECT embedding, vector_payload, source_id FROM chunk WHERE id = :id"),
                {"id": p1.id},
            ).first()
        assert row is not None, "delete_by_ids must not delete the chunk row"
        assert row[0] is None and row[1] is None
        assert row[2] == "s1", "delete_by_ids must leave source_id/acl/is_current alone"

    def test_delete_by_ids_empty_is_noop(self, store, pg_engine, tenant):
        p1, p2, p3, _ = self._setup_with_data(pg_engine, tenant)
        store.upsert([p1, p2, p3])
        store.delete_by_ids([])
        assert store.count(tenant) == 3

    def test_delete_by_document(self, store, pg_engine, tenant):
        p1, p2, p3, _ = self._setup_with_data(pg_engine, tenant)
        store.upsert([p1, p2, p3])

        store.delete_by_document(tenant, "d1")
        assert store.count(tenant) == 1
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(_vec(0.5, 0.5), limit=10, flt=flt)
        assert {h.id for h in hits} == {p3.id}

    def test_delete_by_versions(self, store, pg_engine, tenant):
        p1, p2, p3, shared_version = self._setup_with_data(pg_engine, tenant)
        store.upsert([p1, p2, p3])

        store.delete_by_versions(tenant, [shared_version])
        assert store.count(tenant) == 1
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(_vec(0.5, 0.5), limit=10, flt=flt)
        assert {h.id for h in hits} == {p3.id}

    def test_delete_by_versions_empty_is_noop(self, store, pg_engine, tenant):
        p1, p2, p3, _ = self._setup_with_data(pg_engine, tenant)
        store.upsert([p1, p2, p3])
        store.delete_by_versions(tenant, [])
        assert store.count(tenant) == 3


# ---------------------------------------------------------------------------
# 5. search_dense
# ---------------------------------------------------------------------------


class TestSearchDense:
    def test_returns_sorted_by_score_descending(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, dense=_vec(0.0, 1.0))
        p3 = _seed_point(pg_engine, tenant, dense=_vec(0.7, 0.7))
        store.upsert([p1, p2, p3])

        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(_vec(1.0, 0.0), limit=3, flt=flt)
        assert [h.id for h in hits] == [p1.id, p3.id, p2.id]
        assert hits[0].score >= hits[1].score >= hits[2].score

    def test_tenant_filter_pushed_down(self, store, pg_engine, tenant):
        other_tenant = f"pgvec-{_new_suffix()}"
        p1 = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, other_tenant, dense=_vec(1.0, 0.0))
        store.upsert([p1, p2])
        try:
            flt = SearchFilter(tenant_id=tenant)
            hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
            assert {h.id for h in hits} == {p1.id}
        finally:
            with pg_engine.begin() as conn:
                conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": other_tenant})

    def test_document_id_filter_pushed_down(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, document_id="d1", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, document_id="d2", dense=_vec(1.0, 0.0))
        store.upsert([p1, p2])

        flt = SearchFilter(tenant_id=tenant, document_ids=["d1"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p1.id}

    def test_source_id_filter_pushed_down(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, source_id="s1", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, source_id="s2", dense=_vec(1.0, 0.0))
        store.upsert([p1, p2])

        flt = SearchFilter(tenant_id=tenant, source_ids=["s1"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p1.id}

    def test_kind_filter_pushed_down(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, kind="section", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, kind="table", dense=_vec(1.0, 0.0))
        store.upsert([p1, p2])

        flt = SearchFilter(tenant_id=tenant, kinds=["table"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p2.id}

    def test_current_only_filter(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, is_current=True, dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, is_current=False, dense=_vec(1.0, 0.0))
        store.upsert([p1, p2])

        flt = SearchFilter(tenant_id=tenant, current_only=True)
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p1.id}

        flt_all = SearchFilter(tenant_id=tenant, current_only=False)
        hits_all = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt_all)
        assert {h.id for h in hits_all} == {p1.id, p2.id}

    def test_acl_any_is_array_overlap_pushed_down(self, store, pg_engine, tenant):
        """acl is text[] + GIN, not scalar + btree (Landmine 3): a chunk
        whose acl list *overlaps* the filter's acl_any must match, and this
        must be pushed into the SQL - not post-filtered in Python the way
        SqliteVecStore has to (vec0 cannot hold array columns)."""
        p_public = _seed_point(pg_engine, tenant, acl=["public"], dense=_vec(1.0, 0.0))
        p_multi = _seed_point(
            pg_engine, tenant, acl=["internal", "public"], dense=_vec(1.0, 0.0)
        )
        p_private = _seed_point(pg_engine, tenant, acl=["internal"], dense=_vec(1.0, 0.0))
        store.upsert([p_public, p_multi, p_private])

        flt = SearchFilter(tenant_id=tenant, acl_any=["public"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p_public.id, p_multi.id}

    def test_empty_collection_returns_empty(self, store, tenant):
        flt = SearchFilter(tenant_id=tenant)
        assert store.search_dense(_vec(1.0), limit=10, flt=flt) == []

    def test_deleted_rows_are_excluded(self, store, pg_engine, tenant):
        """embedding IS NOT NULL must actually be enforced - a row whose
        vector was nulled by a delete_by_* call must never resurface."""
        point = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        store.upsert([point])
        store.delete_by_ids([point.id])

        flt = SearchFilter(tenant_id=tenant)
        assert store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt) == []

    def test_payload_returned(self, store, pg_engine, tenant):
        point = _seed_point(
            pg_engine, tenant, document_id="d7", text_="贼克者取用之首法也", dense=_vec(1.0)
        )
        point.payload["heading_path"] = ["卷一", "总说"]
        store.upsert([point])

        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(_vec(1.0), limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "贼克者取用之首法也"
        assert hits[0].payload["document_id"] == "d7"
        assert hits[0].payload["heading_path"] == ["卷一", "总说"]

    def test_multi_valued_document_ids_filter_matches_either(self, store, pg_engine, tenant):
        """A 2+ element `document_ids` list must match rows from *either*
        document - the shape that turned out to need the planner override
        in `TestExactScanOverride` below (this test proves the SQL contract
        at small scale; that class proves the fix actually engages)."""
        p1 = _seed_point(pg_engine, tenant, document_id="d1", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, document_id="d2", dense=_vec(1.0, 0.0))
        p3 = _seed_point(pg_engine, tenant, document_id="d3", dense=_vec(1.0, 0.0))
        store.upsert([p1, p2, p3])

        flt = SearchFilter(tenant_id=tenant, document_ids=["d1", "d2"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p1.id, p2.id}

    def test_multi_valued_source_ids_filter_matches_either(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, source_id="s1", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, source_id="s2", dense=_vec(1.0, 0.0))
        p3 = _seed_point(pg_engine, tenant, source_id="s3", dense=_vec(1.0, 0.0))
        store.upsert([p1, p2, p3])

        flt = SearchFilter(tenant_id=tenant, source_ids=["s1", "s2"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p1.id, p2.id}

    def test_multi_valued_kinds_filter_matches_either(self, store, pg_engine, tenant):
        p1 = _seed_point(pg_engine, tenant, kind="section", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, kind="table", dense=_vec(1.0, 0.0))
        p3 = _seed_point(pg_engine, tenant, kind="figure", dense=_vec(1.0, 0.0))
        store.upsert([p1, p2, p3])

        flt = SearchFilter(tenant_id=tenant, kinds=["section", "table"])
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert {h.id for h in hits} == {p1.id, p2.id}


# ---------------------------------------------------------------------------
# 6. Regression: multi-valued/array filters must force the planner off the
# HNSW-ignores-the-filter path (discovered via the ticket's own required
# middle-selectivity measurement, not in the ticket text - see the module
# docstring and ticket 06's Comments for the full investigation).
# ---------------------------------------------------------------------------


class TestExactScanOverride:
    """Structural proof the `SET LOCAL enable_indexscan = off` override
    engages exactly when it needs to: captures the statements `search_dense`
    actually sends via SQLAlchemy's `before_cursor_execute` event, rather
    than relying on the planner happening to misbehave at whatever scale a
    fast test can afford - the 100k benchmark's re-measured mid-selectivity
    tier is what proves this fixes the *real* bug end-to-end."""

    def _captured_statements(self, engine, fn) -> list[str]:
        from sqlalchemy import event

        statements: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _capture)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", _capture)
        return statements

    def test_no_filter_does_not_disable_indexscan(self, store, pg_engine, tenant):
        point = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        store.upsert([point])

        flt = SearchFilter(tenant_id=tenant)
        statements = self._captured_statements(
            store._engine, lambda: store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        )
        assert not any("enable_indexscan" in s for s in statements)

    def test_multi_valued_document_ids_filter_disables_indexscan(self, store, pg_engine, tenant):
        point = _seed_point(pg_engine, tenant, document_id="d1", dense=_vec(1.0, 0.0))
        store.upsert([point])

        flt = SearchFilter(tenant_id=tenant, document_ids=["d1", "d2"])
        statements = self._captured_statements(
            store._engine, lambda: store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        )
        assert any("enable_indexscan" in s for s in statements)

    def test_single_valued_document_ids_filter_stays_on_the_fast_path(
        self, store, pg_engine, tenant
    ):
        """A single document id is exactly the ticket's own primary "narrow
        filter" scenario (`document_id = 'doc7'`) - measured as safe under
        the default planner even via `= ANY(...)`, so it must not pay the
        override's cost. Locks in the length check in `search_dense` against
        a future edit accidentally making this unconditional again."""
        point = _seed_point(pg_engine, tenant, document_id="d1", dense=_vec(1.0, 0.0))
        store.upsert([point])

        flt = SearchFilter(tenant_id=tenant, document_ids=["d1"])
        statements = self._captured_statements(
            store._engine, lambda: store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        )
        assert not any("enable_indexscan" in s for s in statements)

    def test_acl_any_filter_disables_indexscan(self, store, pg_engine, tenant):
        point = _seed_point(pg_engine, tenant, acl=["public"], dense=_vec(1.0, 0.0))
        store.upsert([point])

        flt = SearchFilter(tenant_id=tenant, acl_any=["public"])
        statements = self._captured_statements(
            store._engine, lambda: store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        )
        assert any("enable_indexscan" in s for s in statements)

    def test_indexscan_override_is_scoped_to_one_transaction(self, store, pg_engine, tenant):
        """`SET LOCAL` must not leak: a query right after a filtered one,
        with no filter of its own, must not inherit the override."""
        p1 = _seed_point(pg_engine, tenant, document_id="d1", dense=_vec(1.0, 0.0))
        p2 = _seed_point(pg_engine, tenant, document_id="d2", dense=_vec(0.0, 1.0))
        store.upsert([p1, p2])

        store.search_dense(
            _vec(1.0, 0.0), limit=10, flt=SearchFilter(tenant_id=tenant, document_ids=["d1", "d2"])
        )
        statements = self._captured_statements(
            store._engine,
            lambda: store.search_dense(
                _vec(1.0, 0.0), limit=10, flt=SearchFilter(tenant_id=tenant)
            ),
        )
        assert not any("enable_indexscan" in s for s in statements), (
            "SET LOCAL from a previous filtered query leaked into an unfiltered one"
        )


# ---------------------------------------------------------------------------
# 7. Transactional writes (acceptance criterion: participates in caller-
#    managed transactions, never commits/rolls back itself)
# ---------------------------------------------------------------------------


class TestTransactionalWrites:
    def test_upsert_rolls_back_with_session(self, store, pg_engine, tenant):
        point = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))

        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.upsert([point], session=session)
            session.rollback()
        finally:
            session.close()

        assert store.count(tenant) == 0
        flt = SearchFilter(tenant_id=tenant)
        assert store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt) == []

    def test_upsert_commits_with_session(self, store, pg_engine, tenant):
        point = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))

        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.upsert([point], session=session)
            session.commit()
        finally:
            session.close()

        assert store.count(tenant) == 1
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(_vec(1.0, 0.0), limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].id == point.id

    def test_delete_by_ids_rolls_back_with_session(self, store, pg_engine, tenant):
        point = _seed_point(pg_engine, tenant, dense=_vec(1.0, 0.0))
        store.upsert([point])

        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.delete_by_ids([point.id], session=session)
            session.rollback()
        finally:
            session.close()

        assert store.count(tenant) == 1, "delete should have rolled back with the session"


# ---------------------------------------------------------------------------
# 8. Correctness at small scale: HNSW top-k should match Python brute force
#    cosine ranking closely when the corpus is tiny (a cheap sanity check,
#    not a substitute for the 100k Recall@10 benchmark below).
# ---------------------------------------------------------------------------


class TestTopKCorrectness:
    def test_topk_matches_bruteforce_cosine_at_small_scale(self, store, pg_engine, tenant):
        import math
        import random

        random.seed(7)
        points = []
        for _ in range(20):
            vec = [random.uniform(-1, 1) for _ in range(FAST_DIM)]
            points.append(_seed_point(pg_engine, tenant, dense=vec))
        store.upsert(points)

        query = [random.uniform(-1, 1) for _ in range(FAST_DIM)]

        def cosine(a, b):
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb) if na and nb else 0.0

        scored = [(p.id, cosine(query, p.dense)) for p in points]
        scored.sort(key=lambda x: -x[1])
        expected_top5 = {pid for pid, _ in scored[:5]}

        flt = SearchFilter(tenant_id=tenant)
        hits = store.search_dense(query, limit=5, flt=flt)
        actual_top5 = {h.id for h in hits}
        assert actual_top5 == expected_top5, (
            f"pgvector top-5 {actual_top5} != brute-force {expected_top5} "
            "(small enough corpus that HNSW should be exact)"
        )


# ---------------------------------------------------------------------------
# 9. Acceptance: narrow-filter sentinel + EXPLAIN proof (ticket's 2nd box)
# ---------------------------------------------------------------------------


class TestNarrowFilterSentinel:
    """Guards the ticket's headline failure mode: pgvector HNSW under a
    narrow filter with no supporting btree/GIN silently returns an empty
    result set (spike numbers: recall 0.0000, 0 rows, at 100k scale). These
    tests prove the *fixed* implementation - with the six indexes in place -
    genuinely uses them (EXPLAIN) and genuinely returns rows (the sentinel
    itself), at a scale calibrated empirically against the live dev database:
    even 500-1000 rows at ~1% selectivity is already enough for Postgres's
    cost-based planner to prefer the index over a sequential scan. This is a
    different scale threshold than the 100k the recall benchmark below needs
    - that one is specific to HNSW's own bounded-candidate-list blindness,
    not to the planner's index-vs-seqscan cost comparison."""

    N_ROWS = 1000
    N_DOCS = 100

    def test_source_id_narrow_filter_non_empty_and_uses_btree(self, store, pg_engine, tenant):
        _bulk_seed_clustered(
            pg_engine, tenant, n_rows=self.N_ROWS, n_docs=self.N_DOCS, dim=FAST_DIM
        )

        with pg_engine.connect() as conn:
            qid = conn.execute(
                text("SELECT id FROM chunk WHERE tenant_id = :t LIMIT 1"), {"t": tenant}
            ).scalar()

        explain_sql = (
            "SELECT id FROM chunk WHERE embedding IS NOT NULL "
            "AND tenant_id = :tenant AND source_id = :source_id AND is_current = true "
            "ORDER BY embedding <=> (SELECT embedding FROM chunk WHERE id = :qid) LIMIT 10"
        )
        plan_text = _explain(
            pg_engine, explain_sql, {"tenant": tenant, "source_id": "src1", "qid": qid}
        )
        assert "Seq Scan" not in plan_text, f"expected an index-based plan, got:\n{plan_text}"
        assert "ix_chunk_source_id" in plan_text, (
            f"expected the source_id btree in the plan, got:\n{plan_text}"
        )

        flt = SearchFilter(tenant_id=tenant, source_ids=["src1"])
        hits = store.search_dense(_vec(1.0), limit=10, flt=flt)
        assert hits, (
            "narrow source_id filter returned zero rows - this is exactly the "
            "silent-failure mode the ticket's spike measured without the btree"
        )

    def test_acl_narrow_filter_non_empty_and_uses_gin(self, store, pg_engine, tenant):
        _bulk_seed_clustered(
            pg_engine,
            tenant,
            n_rows=self.N_ROWS,
            n_docs=self.N_DOCS,
            dim=FAST_DIM,
            rare_acl_rows=set(range(1, 9)),  # ~0.8% of rows carry 'rare-tag'
        )

        with pg_engine.connect() as conn:
            qid = conn.execute(
                text("SELECT id FROM chunk WHERE tenant_id = :t LIMIT 1"), {"t": tenant}
            ).scalar()

        explain_sql = (
            "SELECT id FROM chunk WHERE embedding IS NOT NULL "
            "AND tenant_id = :tenant AND acl && :acl_any AND is_current = true "
            "ORDER BY embedding <=> (SELECT embedding FROM chunk WHERE id = :qid) LIMIT 10"
        )
        plan_text = _explain(
            pg_engine, explain_sql, {"tenant": tenant, "acl_any": ["rare-tag"], "qid": qid}
        )
        assert "Seq Scan" not in plan_text, f"expected an index-based plan, got:\n{plan_text}"
        assert "ix_chunk_acl_gin" in plan_text, (
            f"expected the acl GIN index in the plan, got:\n{plan_text}"
        )

        flt = SearchFilter(tenant_id=tenant, acl_any=["rare-tag"])
        hits = store.search_dense(_vec(1.0), limit=10, flt=flt)
        assert hits, "narrow acl filter returned zero rows"


# ---------------------------------------------------------------------------
# 10. Acceptance: 100k-scale three-tier Recall@10 + latency (ticket's 3rd/4th
#    boxes). Skipped by default - set KB_RUN_SLOW_TESTS=1 to run.
# ---------------------------------------------------------------------------


_BENCH_DIM = 512
_BENCH_N_ROWS = 100_000
_BENCH_N_DOCS = 202  # 1/202 ~= 0.495%, matching the ticket's own "narrow = one book"
_BENCH_TENANT = "pgvec-bench-100k"
_BENCH_TRIALS = 30


def _bench_populate(engine) -> None:
    """Bulk-load 100k clustered chunks, then build the HNSW index fresh over
    already-populated data (matching `paradedb_fixture.sql`'s bulk-then-index
    order, not incremental per-row maintenance during the insert - the
    former is what the ticket's own "4分34秒" reference number measured)."""
    from kbsvc.db.pgvector_ddl import drop_pgvector_embedding, ensure_pgvector_schema

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": _BENCH_TENANT})

    # Clean slate for `embedding` specifically (drop_pgvector_embedding never
    # touches source_id/acl/is_current/vector_payload or their indexes).
    drop_pgvector_embedding(engine)
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE chunk ADD COLUMN embedding vector({_BENCH_DIM})"))

        conn.execute(text("DROP TABLE IF EXISTS bench_ctr"))
        conn.execute(
            text(
                f"CREATE TABLE bench_ctr AS SELECT k, "
                f"(SELECT array_agg(random_normal())::real[] "
                f"FROM generate_series(1, {_BENCH_DIM} + 0*k)) AS vec "
                f"FROM generate_series(0, {_BENCH_N_DOCS - 1}) k"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chunk (id, tenant_id, document_id, version_id, ordinal, "
                "kind, text, token_count, char_start, char_end, section_id, "
                "heading_path, content_hash, analyzed, created_at, source_id, acl, "
                "is_current, embedding) "
                "SELECT 'bench-' || i, :tenant, 'doc' || (i % :n_docs), "
                "'ver-bench-' || i, 0, 'section', "
                "'common ' || (SELECT string_agg('t' || floor(random() * 5000)::int, ' ') "
                "FROM generate_series(1, 40 + 0*i)), "
                "40, 0, 10, '', '[]', 'h' || i, '', now(), "
                "'src' || (i % :n_docs), "
                "CASE WHEN i % 10 = 0 THEN ARRAY['private'] ELSE ARRAY['public'] END, true, "
                "l2_normalize((SELECT array_agg(v * 1.53 + random_normal()) "
                "FROM unnest((SELECT vec FROM bench_ctr WHERE k = i % :n_docs)) "
                "AS u(v))::real[]::vector) "
                "FROM generate_series(1, :n_rows) i"
            ),
            {"tenant": _BENCH_TENANT, "n_docs": _BENCH_N_DOCS, "n_rows": _BENCH_N_ROWS},
        )
        conn.execute(text("DROP TABLE bench_ctr"))

    # HNSW build happens here, as a batch CREATE INDEX over 100k already-
    # populated rows - ensure_pgvector_schema's other ALTERs/indexes are all
    # no-ops at this point (already exist from earlier tests in the module).
    ensure_pgvector_schema(engine, dim=_BENCH_DIM)
    with engine.begin() as conn:
        conn.execute(text("ANALYZE chunk"))


def _bench_measure(engine, *, filter_sql: str, approx_force_exact: bool = False) -> dict:
    """Mirrors `.scratch/storage-consolidation/spike_06_recall.sql`'s
    `spike_recall()`: for each trial, pick a query vector from the corpus
    itself, run the same KNN query once with `enable_indexscan/bitmapscan`
    off (exact ground truth) and once with them on (the real HNSW path),
    and compare the resulting id sets.

    `approx_force_exact` reproduces what `PgVectorStore.search_dense` itself
    now does for a multi-valued/array filter (`needs_exact_scan` in that
    module): `SET LOCAL enable_indexscan = off` on the "approx" leg too,
    leaving `enable_bitmapscan` on. Default `false` for the three tiers that
    mirror the ticket's spike verbatim (unfiltered, single-tag acl, single
    document) - those go through the store's fast path unmodified. `true`
    for the "with the fix" mid-tier measurement, to report what
    `PgVectorStore.search_dense` actually returns in production, not the raw
    planner behaviour the bug was found in.
    """
    sql = text(
        f"SELECT array_agg(id) FROM (SELECT id FROM chunk WHERE {filter_sql} "
        "AND tenant_id = :tenant AND embedding IS NOT NULL "
        "ORDER BY embedding <=> (SELECT embedding FROM chunk WHERE id = :qid) "
        "LIMIT 10) s"
    )
    total_hits = 0
    total_returned = 0
    exact_ms = 0.0
    approx_ms = 0.0
    with engine.begin() as conn:
        for i in range(1, _BENCH_TRIALS + 1):
            qid = f"bench-{(i * 9973) % _BENCH_N_ROWS + 1}"
            params = {"tenant": _BENCH_TENANT, "qid": qid}

            conn.execute(text("SET LOCAL enable_indexscan = off"))
            conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            t0 = time.perf_counter()
            exact_ids = conn.execute(sql, params).scalar() or []
            exact_ms += (time.perf_counter() - t0) * 1000

            conn.execute(
                text(f"SET LOCAL enable_indexscan = {'off' if approx_force_exact else 'on'}")
            )
            conn.execute(text("SET LOCAL enable_bitmapscan = on"))
            t0 = time.perf_counter()
            approx_ids = conn.execute(sql, params).scalar() or []
            approx_ms += (time.perf_counter() - t0) * 1000

            total_hits += len(set(approx_ids) & set(exact_ids))
            total_returned += len(approx_ids)

    return {
        "recall": round(total_hits / (_BENCH_TRIALS * 10), 4),
        "returned": round(total_returned / _BENCH_TRIALS, 2),
        "approx_ms": round(approx_ms / _BENCH_TRIALS, 2),
        "exact_ms": round(exact_ms / _BENCH_TRIALS, 2),
    }


class TestHundredKThreeTierBenchmark:
    def test_recall_and_latency_across_three_selectivity_tiers(self, pg_engine, _pg_env):
        """100k chunks x 512 dims. Skipped by default (set KB_RUN_SLOW_TESTS=1
        to run) - mirrors `tests/test_sqlite_vec_store.py`'s
        `test_100k_search_under_150ms` gating convention exactly. Populating
        100k rows and building the HNSW index takes several minutes."""
        if not os.environ.get("KB_RUN_SLOW_TESTS"):
            pytest.skip("set KB_RUN_SLOW_TESTS=1 to run the 100k three-tier benchmark")

        from kbsvc.vector.pgvector_store import PgVectorStore

        t0 = time.perf_counter()
        _bench_populate(pg_engine)
        setup_s = time.perf_counter() - t0

        try:
            mid_docs = [f"doc{i}" for i in range(20)]  # 20/202 ~= 9.90%
            mid_filter_sql = f"document_id = ANY(ARRAY{mid_docs!r})"

            results = {
                "无过滤 (100%)": _bench_measure(pg_engine, filter_sql="true"),
                "宽过滤 acl (90%)": _bench_measure(pg_engine, filter_sql="acl && ARRAY['public']"),
                "中等过滤 documents (~10%, 默认planner)": _bench_measure(
                    pg_engine, filter_sql=mid_filter_sql
                ),
                "中等过滤 documents (~10%, 加SET LOCAL修复)": _bench_measure(
                    pg_engine, filter_sql=mid_filter_sql, approx_force_exact=True
                ),
                "窄过滤 单本书 (0.5%)": _bench_measure(
                    pg_engine, filter_sql="document_id = 'doc7'"
                ),
            }

            print(f"\n=== pgvector 100k x {_BENCH_DIM}d benchmark (setup {setup_s:.1f}s) ===")
            for label, m in results.items():
                print(
                    f"{label}: recall={m['recall']:.4f} returned={m['returned']:.2f} "
                    f"approx={m['approx_ms']:.2f}ms exact={m['exact_ms']:.2f}ms"
                )

            # Ticket acceptance: narrow filter must not be a pathological
            # outlier relative to the other tiers (the ticket's own spike
            # found narrow recall *higher* than unfiltered once the btree is
            # present - 1.000 vs 0.963 - because the exact-sort fallback has
            # no HNSW approximation error at all within the filtered subset).
            narrow = results["窄过滤 单本书 (0.5%)"]
            assert narrow["returned"] > 0, (
                "narrow filter returned zero rows on average - the exact "
                "silent-failure mode the ticket's spike measured"
            )

            # The mid-tier's *default-planner* number is a locked-in
            # regression guard for the second failure mode this ticket
            # found (see the module docstring): it must stay bad. If a
            # future Postgres/pgvector upgrade fixes the planner's cost
            # estimate for multi-valued filters, this assertion starts
            # failing and is the signal that `needs_exact_scan` in
            # `PgVectorStore.search_dense` may no longer be needed - it is
            # NOT a bug in this test if that day comes.
            mid_default = results["中等过滤 documents (~10%, 默认planner)"]
            assert mid_default["recall"] < 0.5, (
                f"mid-tier default-planner recall {mid_default['recall']} recovered - if "
                "Postgres/pgvector fixed this, `needs_exact_scan` may no longer be needed"
            )

            # The mid-tier's *fixed* number is what PgVectorStore.search_dense
            # actually returns in production for this filter shape (20
            # document ids) - this is the real acceptance bar.
            for label in (
                "无过滤 (100%)",
                "宽过滤 acl (90%)",
                "中等过滤 documents (~10%, 加SET LOCAL修复)",
                "窄过滤 单本书 (0.5%)",
            ):
                m = results[label]
                assert m["recall"] >= 0.9, f"{label} recall {m['recall']} is below 0.9"
        finally:
            with pg_engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": _BENCH_TENANT}
                )
            # Restore FAST_DIM so any other test in this module - or a later
            # invocation without KB_RUN_SLOW_TESTS - does not inherit a
            # dim=512 column and fail ensure_collection's mismatch check.
            PgVectorStore().recreate_collection(FAST_DIM)

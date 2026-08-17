"""Tests for PgSearchLexicalStore (ADR-0008 ticket 07).

Covers all 10 LexicalStore Protocol methods plus the ticket's acceptance
criteria:
- ensure_ready adds body/lexical_payload + the shared source_id/acl/is_current
  columns and the bm25 index, idempotently
- upsert is UPDATE-shaped: requires a pre-existing base `chunk` row and
  silently no-ops when that row does not exist (same contract as
  PgVectorStore.upsert)
- delete_by_* null body/lexical_payload and never delete the row itself
- search pushes every SearchFilter field down through pg_search's query DSL
  (paradedb.boolean), not an SQL WHERE
- an EXPLAIN-based proof the DSL really compiles to a single Tantivy index
  scan and does not degrade to a `heap_filter` step
- top-k set equality with TantivyLexicalStore at small scale (the ticket's
  own framing: same engine, different index location - this should pass
  without the IDF-clamp divergence FTS5 has)
- two independent connections writing disjoint chunk ids concurrently do
  not error or lose writes
- caller-managed sessions roll back and commit cleanly
- bulk() defers visibility across many writes until the block exits

Runs against a real PostgreSQL/ParadeDB instance - point KB_TEST_POSTGRES_URL
at one, or this whole module skips cleanly (mirrors
`tests/test_postgres_extensions.py` and `tests/test_pgvector_store.py`).

Test isolation note: the `chunk` table is shared and persists across test
runs, unlike SQLite's per-test tmp file. `uq_chunk_position` is a UNIQUE
constraint on `(version_id, ordinal)` with no tenant_id in it, so every
helper here mints a fresh globally-unique `version_id` per chunk rather than
reusing short literals like `"v1"` the way the SQLite-backed suites safely
do (same convention as `tests/test_pgvector_store.py`).
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from kbsvc.errors import KbError
from kbsvc.lexical.base import LexicalDocument, SearchFilter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_URL = "postgresql+psycopg://kbsvc:kbsvc@postgres:5432/kbsvc_test"

_SKIP_REASON = (
    "no PostgreSQL reachable at KB_TEST_POSTGRES_URL ({url}): {error}. "
    "ADR-0008 ticket 07's PgSearchLexicalStore assertions did NOT run."
)


def _target_url() -> str:
    return os.environ.get("KB_TEST_POSTGRES_URL", _DEFAULT_URL)


@pytest.fixture(scope="module")
def pg_engine():
    """Engine against the test database, or a loud skip. Mirrors
    `tests/test_pgvector_store.py`'s `pg_engine` fixture exactly."""
    from sqlalchemy import create_engine

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
    the whole module. Mirrors `tests/test_pgvector_store.py`'s fixture of the
    same name exactly (module-scoped `pytest.MonkeyPatch`, because the
    function-scoped `monkeypatch` fixture cannot back a module-scoped one)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("KB_DATABASE_URL", pg_engine.url.render_as_string(hide_password=False))
    mp.setenv("KB_PROFILE", "server")
    mp.setenv("KB_LEXICAL_BACKEND", "pg-search")
    from kbsvc.config import reset_settings_cache
    from kbsvc.db.session import reset_engine_cache

    reset_settings_cache()
    reset_engine_cache()
    yield
    mp.undo()
    reset_settings_cache()
    reset_engine_cache()


@pytest.fixture
def store(pg_engine, _pg_env):
    """A PgSearchLexicalStore pointed at the live test Postgres, with the
    bm25 index ensured."""
    from kbsvc.lexical.pg_search_store import PgSearchLexicalStore

    s = PgSearchLexicalStore()
    s.ensure_ready()
    return s


@pytest.fixture
def tenant(pg_engine):
    """A fresh, globally-unique tenant_id per test, with its `chunk` rows
    cleaned up afterward. The dev database persists across runs."""
    tid = f"pgsearch-{uuid.uuid4().hex[:12]}"
    yield tid
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM chunk WHERE tenant_id = :t"), {"t": tid})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_suffix() -> str:
    return uuid.uuid4().hex[:12]


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
    `ingest/worker.py` ever calls `store.upsert`. Same shape as the helper of
    the same name in `tests/test_pgvector_store.py`."""
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


def _make_doc(
    chunk_id: str,
    text_: str,
    *,
    tenant_id: str,
    document_id: str = "d1",
    version_id: str | None = None,
    source_id: str = "s1",
    kind: str = "section",
    acl: list[str] | None = None,
    is_current: bool = True,
) -> LexicalDocument:
    return LexicalDocument(
        id=chunk_id,
        text=text_,
        payload={
            "tenant_id": tenant_id,
            "document_id": document_id,
            "version_id": version_id or f"v-{_new_suffix()}",
            "source_id": source_id,
            "kind": kind,
            "acl": acl if acl is not None else ["public"],
            "is_current": is_current,
            "text": text_,
        },
    )


def _seed_doc(
    pg_engine,
    tenant_id: str,
    text_: str,
    *,
    document_id: str = "d1",
    source_id: str = "s1",
    kind: str = "section",
    acl: list[str] | None = None,
    is_current: bool = True,
    ordinal: int = 0,
    version_id: str | None = None,
) -> LexicalDocument:
    """Insert the base `chunk` row and return the matching `LexicalDocument`
    - the shape every write test calls `store.upsert` with, per the module
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
    return _make_doc(
        chunk_id,
        text_,
        tenant_id=tenant_id,
        document_id=document_id,
        version_id=version_id,
        source_id=source_id,
        kind=kind,
        acl=acl,
        is_current=is_current,
    )


def _explain(pg_engine, sql: str, params: dict) -> str:
    with pg_engine.connect() as conn:
        rows = conn.execute(text(f"EXPLAIN {sql}"), params).fetchall()
    return "\n".join(row[0] for row in rows)


# ---------------------------------------------------------------------------
# 1. ensure_ready / lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ensure_ready_adds_columns_and_bm25_index(self, store, pg_engine):
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
        # body/lexical_payload are this store's own columns; source_id/acl/
        # is_current are the shared-with-pgvector metadata columns.
        assert {"body", "lexical_payload", "source_id", "acl", "is_current"} <= cols
        assert "chunk_bm25" in indexes

    def test_ensure_ready_idempotent(self, store):
        store.ensure_ready()  # must not raise the second time

    def test_close_is_a_noop(self, store):
        # close() must not error and must not disable further use - the
        # engine is shared/owned by db.session, identical to PgVectorStore
        # and Fts5LexicalStore. Asserting idempotency (calling twice) is
        # the contract; there is no internal connection pool to inspect.
        store.close()
        store.close()


# ---------------------------------------------------------------------------
# 2. upsert + count
# ---------------------------------------------------------------------------


class TestUpsertAndCount:
    def test_upsert_and_count(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "之乎者也")
        d2 = _seed_doc(pg_engine, tenant, "风马牛不相及")
        store.upsert([d1, d2])
        assert store.count(tenant) == 2

    def test_count_without_tenant_id_counts_every_tenant(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "之乎者也")
        d2 = _seed_doc(pg_engine, tenant, "风马牛不相及")
        store.upsert([d1, d2])
        assert store.count() >= 2
        assert store.count() >= store.count(tenant)

    def test_upsert_updates_the_existing_row_rather_than_inserting(
        self, store, pg_engine, tenant
    ):
        doc = _seed_doc(pg_engine, tenant, "old body text", kind="section")
        store.upsert([doc])

        updated = _make_doc(
            doc.id,
            "new body text",
            tenant_id=tenant,
            document_id=doc.payload["document_id"],
            version_id=doc.payload["version_id"],
            kind="table",
        )
        store.upsert([updated])
        assert store.count(tenant) == 1

        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("new", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "new body text"
        assert hits[0].payload["kind"] == "table"

    def test_upsert_on_missing_base_row_is_a_silent_noop(self, store, tenant):
        """The base `chunk` row must already exist (module docstring) -
        `store.upsert` is never the thing that creates it. Calling it for an
        id with no matching row must not error and must not create a row."""
        orphan = _make_doc(
            f"missing-{_new_suffix()}", "orphan body", tenant_id=tenant
        )
        store.upsert([orphan])  # must not raise
        assert store.count(tenant) == 0

    def test_upsert_empty_list_is_noop(self, store, tenant):
        store.upsert([])
        assert store.count(tenant) == 0


# ---------------------------------------------------------------------------
# 3. Deletes
# ---------------------------------------------------------------------------


class TestDeletes:
    def _setup_with_data(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "alpha content", document_id="d1")
        d2 = _seed_doc(pg_engine, tenant, "beta content", document_id="d1")
        d3 = _seed_doc(pg_engine, tenant, "gamma content", document_id="d2")
        store.upsert([d1, d2, d3])
        return d1, d2, d3

    def test_delete_by_ids_nulls_body_but_keeps_the_row(
        self, store, pg_engine, tenant
    ):
        d1, d2, d3 = self._setup_with_data(store, pg_engine, tenant)
        store.delete_by_ids([d1.id])
        assert store.count(tenant) == 2

        with pg_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT text, body, lexical_payload, source_id "
                    "FROM chunk WHERE id = :id"
                ),
                {"id": d1.id},
            ).first()
        assert row is not None, "delete_by_ids must not delete the chunk row"
        assert row[0] == "alpha content", "canonical chunk text must survive"
        assert row[1] is None and row[2] is None
        assert row[3] == "s1", "delete_by_ids must leave source_id/acl/is_current alone"

    def test_delete_by_ids_empty_is_noop(self, store, pg_engine, tenant):
        self._setup_with_data(store, pg_engine, tenant)
        store.delete_by_ids([])
        assert store.count(tenant) == 3

    def test_delete_by_document(self, store, pg_engine, tenant):
        d1, d2, d3 = self._setup_with_data(store, pg_engine, tenant)
        store.delete_by_document(tenant, "d1")
        assert store.count(tenant) == 1
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("gamma", limit=10, flt=flt)
        assert {h.id for h in hits} == {d3.id}

    def test_delete_by_versions(self, store, pg_engine, tenant):
        # Two chunks share a version_id so the version-scoped delete hits
        # both; the third has its own version and survives.
        shared_version = f"v-{_new_suffix()}"
        d1 = _seed_doc(
            pg_engine, tenant, "alpha content", document_id="d1",
            version_id=shared_version, ordinal=0,
        )
        d2 = _seed_doc(
            pg_engine, tenant, "beta content", document_id="d1",
            version_id=shared_version, ordinal=1,
        )
        d3 = _seed_doc(pg_engine, tenant, "gamma content", document_id="d2")
        store.upsert([d1, d2, d3])

        store.delete_by_versions(tenant, [shared_version])
        assert store.count(tenant) == 1
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("gamma", limit=10, flt=flt)
        assert {h.id for h in hits} == {d3.id}

    def test_delete_by_versions_empty_is_noop(self, store, pg_engine, tenant):
        self._setup_with_data(store, pg_engine, tenant)
        store.delete_by_versions(tenant, [])
        assert store.count(tenant) == 3


# ---------------------------------------------------------------------------
# 4. search: basic shape
# ---------------------------------------------------------------------------


class TestSearch:
    def test_basic_term_search(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "之乎者也")
        d2 = _seed_doc(pg_engine, tenant, "风马牛不相及")
        store.upsert([d1, d2])
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("之乎", limit=10, flt=flt)
        assert {h.id for h in hits} == {d1.id}

    def test_returns_sorted_by_score_descending(self, store, pg_engine, tenant):
        # c2 mentions the rare term twice; c1 once. Higher TF must score higher.
        d1 = _seed_doc(pg_engine, tenant, "rareterm appears once here")
        d2 = _seed_doc(pg_engine, tenant, "rareterm appears here too rareterm repeated")
        store.upsert([d1, d2])
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("rareterm", limit=10, flt=flt)
        by_id = {h.id: h.score for h in hits}
        assert by_id[d2.id] > by_id[d1.id]
        assert hits[0].id == d2.id
        assert hits[0].score >= hits[1].score

    def test_empty_query_returns_empty(self, store, pg_engine, tenant):
        d = _seed_doc(pg_engine, tenant, "之乎者也")
        store.upsert([d])
        flt = SearchFilter(tenant_id=tenant)
        assert store.search("", limit=10, flt=flt) == []

    def test_empty_collection_returns_empty(self, store, tenant):
        flt = SearchFilter(tenant_id=tenant)
        assert store.search("hello", limit=10, flt=flt) == []

    def test_deleted_rows_are_excluded(self, store, pg_engine, tenant):
        d = _seed_doc(pg_engine, tenant, "hello world")
        store.upsert([d])
        store.delete_by_ids([d.id])
        flt = SearchFilter(tenant_id=tenant)
        assert store.search("hello", limit=10, flt=flt) == []

    def test_payload_returned(self, store, pg_engine, tenant):
        d = _seed_doc(
            pg_engine, tenant, "贼克者取用之首法也", document_id="d7"
        )
        d.payload["heading_path"] = ["卷一", "总说"]
        store.upsert([d])
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("贼克", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "贼克者取用之首法也"
        assert hits[0].payload["document_id"] == "d7"
        assert hits[0].payload["heading_path"] == ["卷一", "总说"]


# ---------------------------------------------------------------------------
# 5. search: every SearchFilter field pushed into the DSL
# ---------------------------------------------------------------------------


class TestSearchFilters:
    def test_tenant_filter_pushed_down(self, store, pg_engine, tenant):
        other_tenant = f"pgsearch-{_new_suffix()}"
        d1 = _seed_doc(pg_engine, tenant, "hello world")
        d2 = _seed_doc(pg_engine, other_tenant, "hello world")
        store.upsert([d1, d2])
        try:
            flt = SearchFilter(tenant_id=tenant)
            hits = store.search("hello", limit=10, flt=flt)
            assert {h.id for h in hits} == {d1.id}
        finally:
            with pg_engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM chunk WHERE tenant_id = :t"),
                    {"t": other_tenant},
                )

    def test_document_id_filter_pushed_down(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "hello world", document_id="d1")
        d2 = _seed_doc(pg_engine, tenant, "hello world", document_id="d2")
        store.upsert([d1, d2])
        flt = SearchFilter(tenant_id=tenant, document_ids=["d1"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {d1.id}

    def test_source_id_filter_pushed_down(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "hello world", source_id="s1")
        d2 = _seed_doc(pg_engine, tenant, "hello world", source_id="s2")
        store.upsert([d1, d2])
        flt = SearchFilter(tenant_id=tenant, source_ids=["s1"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {d1.id}

    def test_kind_filter_pushed_down(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "hello world", kind="section")
        d2 = _seed_doc(pg_engine, tenant, "hello world", kind="table")
        store.upsert([d1, d2])
        flt = SearchFilter(tenant_id=tenant, kinds=["table"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {d2.id}

    def test_current_only_filter(self, store, pg_engine, tenant):
        d1 = _seed_doc(pg_engine, tenant, "hello world", is_current=True)
        d2 = _seed_doc(pg_engine, tenant, "hello world", is_current=False)
        store.upsert([d1, d2])

        flt = SearchFilter(tenant_id=tenant, current_only=True)
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {d1.id}

        flt_all = SearchFilter(tenant_id=tenant, current_only=False)
        hits_all = store.search("hello", limit=10, flt=flt_all)
        assert {h.id for h in hits_all} == {d1.id, d2.id}

    def test_acl_any_matches_overlap_for_multi_valued_rows(
        self, store, pg_engine, tenant
    ):
        """acl is text[]. A row carrying multiple tags must match a filter
        that asks for any one of them; a row carrying none of them must not
        match (the OR/any-of semantics the module docstring confirms
        empirically for `paradedb.term_set` on an array column)."""
        d_public = _seed_doc(pg_engine, tenant, "hello world", acl=["public"])
        d_multi = _seed_doc(
            pg_engine, tenant, "hello world", acl=["internal", "public"]
        )
        d_private = _seed_doc(
            pg_engine, tenant, "hello world", acl=["internal"]
        )
        store.upsert([d_public, d_multi, d_private])
        flt = SearchFilter(tenant_id=tenant, acl_any=["public"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {d_public.id, d_multi.id}

    def test_multi_valued_document_ids_filter_matches_either(
        self, store, pg_engine, tenant
    ):
        d1 = _seed_doc(pg_engine, tenant, "hello world", document_id="d1")
        d2 = _seed_doc(pg_engine, tenant, "hello world", document_id="d2")
        d3 = _seed_doc(pg_engine, tenant, "hello world", document_id="d3")
        store.upsert([d1, d2, d3])
        flt = SearchFilter(tenant_id=tenant, document_ids=["d1", "d2"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {d1.id, d2.id}


# ---------------------------------------------------------------------------
# 6. Filtering goes through the DSL, never SQL WHERE (ticket acceptance #4)
# ---------------------------------------------------------------------------


class TestFilteringInsideDSL:
    """The ticket's headline performance trap: a filter written as an SQL
    `AND` outside the `@@@` predicate compiles to a `heap_filter` step and
    costs ~4x more than the equivalent `paradedb.boolean` clause. These
    tests prove *structurally* that `build_query` emits the DSL shape and
    that the resulting plan has no `heap_filter` for the filter fields."""

    def test_build_query_wraps_every_filter_inside_paradedb_boolean(self):
        from kbsvc.lexical.pg_search_store import build_query

        flt = SearchFilter(
            tenant_id="t",
            acl_any=["public"],
            document_ids=["d1"],
            source_ids=["s1"],
            kinds=["section"],
            current_only=True,
        )
        dsl, params = build_query(["hello", "world"], flt)
        assert dsl.startswith("paradedb.boolean(must => ARRAY[")
        assert dsl.endswith("])")
        # Each clause is a paradedb call - none escape into SQL AND/WHERE.
        assert "paradedb.term_set('body'" in dsl
        assert "paradedb.term('tenant_id'" in dsl
        assert "paradedb.term('is_current'" in dsl
        assert "paradedb.term_set('acl'" in dsl
        assert "paradedb.term_set('document_id'" in dsl
        assert "paradedb.term_set('source_id'" in dsl
        assert "paradedb.term_set('kind'" in dsl
        # Every value is a bind parameter, not string-interpolated.
        assert params["tenant_id"] == "t"
        assert params["acl_values"] == ["public"]
        assert params["document_id_values"] == ["d1"]
        assert params["source_id_values"] == ["s1"]
        assert params["kind_values"] == ["section"]
        assert params["is_current"] is True
        assert params["terms"] == ["hello", "world"]

    def test_search_sql_has_no_filter_outside_the_dsl(
        self, store, pg_engine, tenant
    ):
        """search() must express every filter inside the `@@@` DSL, not as
        an SQL `AND`/`WHERE` predicate appended outside it. Captures the
        actual statement via SQLAlchemy's `before_cursor_execute` event
        rather than reading the source, so it follows any future internal
        refactor that keeps the contract."""
        from sqlalchemy import event

        d = _seed_doc(pg_engine, tenant, "hello world", document_id="d1")
        store.upsert([d])

        statements: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(store._engine, "before_cursor_execute", _capture)
        try:
            flt = SearchFilter(
                tenant_id=tenant,
                document_ids=["d1"],
                acl_any=["public"],
                current_only=True,
            )
            store.search("hello", limit=10, flt=flt)
        finally:
            event.remove(store._engine, "before_cursor_execute", _capture)

        # The only SELECT that can carry results is the search itself.
        search_stmts = [
            s for s in statements
            if "paradedb.boolean" in s or "paradedb.term" in s
        ]
        assert search_stmts, "no pg_search DSL statement was captured"
        sql_text = search_stmts[-1]
        assert "@@@" in sql_text
        # The WHERE clause must scope entirely to the @@@ predicate.
        # Any SQL AND outside it would be the heap_filter path the ticket
        # warns against. The DSL itself uses commas inside ARRAY[...], not
        # SQL AND, so an " AND " here means a predicate leaked out.
        after_where = sql_text.split("WHERE", 1)[1]
        assert " AND " not in after_where.upper(), (
            f"filter appears to leak outside the @@@ DSL:\n{sql_text}"
        )

    def test_explain_shows_no_heap_filter_for_filter_clauses(
        self, store, pg_engine, tenant
    ):
        """The real proof at the database level: enough rows that pg_search
        would notice a degraded plan, EXPLAIN must not show `heap_filter`
        for the filter fields - they must be pushed into the Tantivy query.
        Scaled empirically like test_pgvector_store's narrow-filter
        sentinel: ~500 rows is enough to exercise the planner."""
        # Bulk-insert enough rows to make the planner's choice meaningful.
        suffix = _new_suffix()
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO chunk (id, tenant_id, document_id, version_id, "
                    "ordinal, kind, text, token_count, char_start, char_end, "
                    "section_id, heading_path, content_hash, created_at, "
                    "source_id, acl, is_current, body, lexical_payload) "
                    "SELECT 'dsl-' || :suffix || '-' || i, :tenant, "
                    "'doc' || (i % 20), 'ver-dsl-' || :suffix || '-' || i, 0, "
                    "'section', 't', 1, 0, 1, '', '[]'::jsonb, 'h' || i, now(), "
                    "'src' || (i % 10), ARRAY['public'], true, "
                    "'common token' || (i % 50), '{}'::jsonb "
                    "FROM generate_series(1, 500) i"
                ),
                {"suffix": suffix, "tenant": tenant},
            )
            conn.execute(text("ANALYZE chunk"))
        try:
            plan = _explain(
                pg_engine,
                "SELECT id FROM chunk WHERE id @@@ paradedb.boolean("
                "must => ARRAY[paradedb.term_set('body', (:terms)::text[]), "
                "paradedb.term('tenant_id', :tenant_id), "
                "paradedb.term_set('document_id', (:docs)::text[])])",
                {
                    "terms": ["token1"],
                    "tenant_id": tenant,
                    "docs": ["doc7"],
                },
            )
            assert "heap_filter" not in plan, (
                f"filter degraded to a heap_filter step:\n{plan}"
            )
        finally:
            with pg_engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM chunk WHERE tenant_id = :t"),
                    {"t": tenant},
                )


# ---------------------------------------------------------------------------
# 7. Transactional writes (acceptance: participates in caller-managed
#    transactions, never commits/rolls back itself)
# ---------------------------------------------------------------------------


class TestTransactionalWrites:
    def test_upsert_rolls_back_with_session(self, store, pg_engine, tenant):
        d = _seed_doc(pg_engine, tenant, "hello world")
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.upsert([d], session=session)
            session.rollback()
        finally:
            session.close()
        assert store.count(tenant) == 0
        flt = SearchFilter(tenant_id=tenant)
        assert store.search("hello", limit=10, flt=flt) == []

    def test_upsert_commits_with_session(self, store, pg_engine, tenant):
        d = _seed_doc(pg_engine, tenant, "hello world")
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.upsert([d], session=session)
            session.commit()
        finally:
            session.close()
        assert store.count(tenant) == 1
        flt = SearchFilter(tenant_id=tenant)
        hits = store.search("hello", limit=10, flt=flt)
        assert len(hits) == 1 and hits[0].id == d.id

    def test_delete_by_ids_rolls_back_with_session(self, store, pg_engine, tenant):
        d = _seed_doc(pg_engine, tenant, "hello world")
        store.upsert([d])
        factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.delete_by_ids([d.id], session=session)
            session.rollback()
        finally:
            session.close()
        assert store.count(tenant) == 1, "delete should have rolled back with the session"


# ---------------------------------------------------------------------------
# 8. Top-k equality with Tantivy (ticket acceptance #3)
# ---------------------------------------------------------------------------


class TestTopKEqualityWithTantivy:
    """Same data, same query, indexed into both engines -> same top-k id set.

    The ticket's framing is that pg_search embeds Tantivy itself as a
    Postgres index access method, so BM25 scoring is same-family - unlike
    FTS5, where IDF-clamp divergence at df >= 50% is expected and asserted
    separately. Here we assert equality without a df carve-out, on a corpus
    deliberately shaped so every query term stays in the low-df regime.
    """

    def test_topk_set_equals_tantivy(self, store, pg_engine, tenant, tmp_path):
        from kbsvc.config import Settings
        from kbsvc.lexical.tantivy_store import TantivyLexicalStore

        # 麒麟 appears in 2 of 6 documents (df = 33%); the other four are
        # unrelated classical-poetry filler. Same corpus shape as the
        # equivalent FTS5 test, minus that test's high-df divergence case.
        docs = [
            _seed_doc(pg_engine, tenant, "麒麟出没于山林之间", document_id="d1"),
            _seed_doc(pg_engine, tenant, "凤凰麒麟皆为祥瑞之兽麒麟", document_id="d2"),
            _seed_doc(pg_engine, tenant, "春风又绿江南岸", document_id="d3"),
            _seed_doc(pg_engine, tenant, "明月几时有把酒问青天", document_id="d4"),
            _seed_doc(pg_engine, tenant, "大江东去浪淘尽千古风流人物", document_id="d5"),
            _seed_doc(pg_engine, tenant, "会当凌绝顶一览众山小", document_id="d6"),
        ]
        store.upsert(docs)

        tantivy_settings = Settings(
            profile="local",
            data_dir=tmp_path,
            lexical_backend="tantivy",
            dense_provider="hash",
        )
        tantivy = TantivyLexicalStore(settings=tantivy_settings)
        tantivy.ensure_ready()
        try:
            # Tantivy gets the same payloads - no base-row/UPDATE distinction
            # there, so build fresh LexicalDocuments with matching ids.
            tantivy.upsert(
                [
                    LexicalDocument(id=d.id, text=d.text, payload=d.payload)
                    for d in docs
                ]
            )
            flt = SearchFilter(tenant_id=tenant)
            pg_hits = store.search("麒麟", limit=6, flt=flt)
            tan_hits = tantivy.search("麒麟", limit=6, flt=flt)
            pg_ids = {h.id for h in pg_hits}
            tan_ids = {h.id for h in tan_hits}
            assert pg_ids == tan_ids == {docs[0].id, docs[1].id}, (
                f"pg_search top-k {pg_ids} != tantivy top-k {tan_ids}"
            )
        finally:
            tantivy.close()


# ---------------------------------------------------------------------------
# 9. Concurrent writers (ticket acceptance #5)
# ---------------------------------------------------------------------------


class TestConcurrentWriters:
    """Two independent connections/threads upserting disjoint chunk ids at
    the same time must not error, not block, and not lose writes. This is
    the property that lifts the Tantivy directory-lock cap; full multi-
    worker deployment validation is ticket 08's job."""

    def test_two_threads_writing_disjoint_ids_both_survive(
        self, store, pg_engine, tenant
    ):
        n_per_thread = 25
        docs_a = [
            _seed_doc(pg_engine, tenant, f"alpha content {i}", source_id="sa")
            for i in range(n_per_thread)
        ]
        docs_b = [
            _seed_doc(pg_engine, tenant, f"beta content {i}", source_id="sb")
            for i in range(n_per_thread)
        ]
        errors: list[BaseException] = []

        def _writer(batch: list[LexicalDocument]) -> None:
            try:
                store.upsert(batch)
            except BaseException as exc:  # noqa: BLE001 - captured, not swallowed
                errors.append(exc)

        t1 = threading.Thread(target=_writer, args=(docs_a,))
        t2 = threading.Thread(target=_writer, args=(docs_b,))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not errors, f"concurrent upsert raised: {errors!r}"

        assert store.count(tenant) == 2 * n_per_thread
        flt_a = SearchFilter(tenant_id=tenant, source_ids=["sa"])
        flt_b = SearchFilter(tenant_id=tenant, source_ids=["sb"])
        assert len(store.search("alpha", limit=n_per_thread + 5, flt=flt_a)) == n_per_thread
        assert len(store.search("beta", limit=n_per_thread + 5, flt=flt_b)) == n_per_thread


# ---------------------------------------------------------------------------
# 10. bulk()
# ---------------------------------------------------------------------------


class TestBulkContextManager:
    def test_bulk_upserts_share_one_transaction_until_exit(
        self, store, pg_engine, tenant
    ):
        """Writes inside a bulk() block must not become visible on a
        separate connection until the block exits."""
        d1 = _seed_doc(pg_engine, tenant, "hello world")
        d2 = _seed_doc(pg_engine, tenant, "more text")
        with store.bulk():
            store.upsert([d1])
            with pg_engine.connect() as conn:
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM chunk WHERE body IS NOT NULL "
                        "AND tenant_id = :t"
                    ),
                    {"t": tenant},
                ).scalar()
            assert count == 0, "bulk() write became visible before the block exited"
            store.upsert([d2])
            with pg_engine.connect() as conn:
                count = conn.execute(
                    text(
                        "SELECT count(*) FROM chunk WHERE body IS NOT NULL "
                        "AND tenant_id = :t"
                    ),
                    {"t": tenant},
                ).scalar()
            assert count == 0
        assert store.count(tenant) == 2

    def test_bulk_does_not_nest(self, store):
        with pytest.raises(KbError, match="does not nest"), store.bulk(), store.bulk():
            pass

    def test_bulk_rolls_back_all_writes_on_exception(self, store, pg_engine, tenant):
        d = _seed_doc(pg_engine, tenant, "hello world")

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom), store.bulk():
            store.upsert([d])
            raise _Boom()
        assert store.count(tenant) == 0

    def test_explicit_session_is_not_redirected_into_the_bulk_connection(
        self, store
    ):
        """A session passed explicitly to a write method must be used
        directly, not silently redirected into the open bulk connection."""
        seen: list[bool] = []
        with store.bulk():
            store._execute_write(
                lambda conn: seen.append(conn is store._bulk_conn), session=None
            )
            assert seen == [True]
            sentinel = object()
            store._execute_write(
                lambda conn: seen.append(conn is sentinel), session=sentinel
            )
            assert seen == [True, True]


# ---------------------------------------------------------------------------
# 11. recreate
# ---------------------------------------------------------------------------


class TestRecreate:
    def test_recreate_empties_all_lexical_data_but_keeps_rows(
        self, store, pg_engine, tenant
    ):
        d1 = _seed_doc(pg_engine, tenant, "hello")
        d2 = _seed_doc(pg_engine, tenant, "world")
        store.upsert([d1, d2])
        assert store.count(tenant) == 2

        store.recreate()
        assert store.count(tenant) == 0

        # The chunk rows themselves still exist and are still usable after
        # a re-upsert.
        with pg_engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM chunk WHERE tenant_id = :t"),
                {"t": tenant},
            ).scalar()
        assert count == 2

        store.upsert([d1])
        assert store.count(tenant) == 1

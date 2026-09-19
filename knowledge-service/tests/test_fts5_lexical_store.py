"""Tests for Fts5LexicalStore (ADR-0008 ticket 04).

Covers all 10 LexicalStore Protocol methods plus the acceptance criteria:
- top-k set equality with TantivyLexicalStore, for df < 50% queries
- divergence from Tantivy's BM25 is asserted to *exist* for df >= 50% queries
  (FTS5 clamps IDF to 1e-06 at that threshold; this is expected, not a bug)
- filtering happens inside MATCH, not a separate SQL WHERE
- 五行 stays a single token; a hyphenated UUID-shaped id filters as an exact
  match, not a fragment match
- upsert under a caller-supplied session rolls back cleanly with the session
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from kbsvc.db.session import _make_engine, reset_engine_cache
from kbsvc.errors import KbError
from kbsvc.lexical.base import LexicalDocument, SearchFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


def _setup_store_env(monkeypatch, tmp_path: Path) -> str:
    """Point the environment at a fresh SQLite DB with the fts5 backend."""
    url = _make_url(tmp_path)
    monkeypatch.setenv("KB_DATABASE_URL", url)
    monkeypatch.setenv("KB_PROFILE", "local")
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KB_LEXICAL_BACKEND", "fts5")
    from kbsvc.config import reset_settings_cache

    reset_settings_cache()
    reset_engine_cache()
    return url


def _make_doc(
    chunk_id: str,
    text_: str,
    *,
    tenant_id: str = "t",
    document_id: str = "d1",
    version_id: str = "v1",
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
            "version_id": version_id,
            "source_id": source_id,
            "kind": kind,
            "acl": acl or ["public"],
            "is_current": is_current,
            "text": text_,
        },
    )


@pytest.fixture(autouse=True)
def _restore_global_environment(monkeypatch):
    """Every test in this file points KB_DATABASE_URL/KB_DATA_DIR/
    KB_LEXICAL_BACKEND at a throwaway per-test database via `monkeypatch`.
    That alone is not enough: `get_settings()`/`get_engine()` are
    `lru_cache`d, and the relative teardown order between two independent
    function-scoped fixtures (this one and pytest's own `monkeypatch`) is not
    guaranteed - so a stale engine pointing at an already-deleted tmp_path
    can otherwise survive past this file's tests and leak into whatever runs
    next in the same session (conftest.py's session-scoped `init_db()` only
    runs once, at the very start). That is exactly what happened the first
    time this suite ran with this file in it: tests/test_ingest.py, which
    assumes `get_settings()` still reflects the global test database, started
    failing with "no such table: chunk".

    Mirrors `test_store_factories.py`'s `select_backend` fixture: call
    `monkeypatch.undo()` explicitly and then reset the caches, both inside
    this fixture's own teardown, so the ordering is guaranteed rather than
    incidental. Calling `monkeypatch.undo()` twice (once here, once via
    pytest's own automatic teardown right after) is safe - it is idempotent.
    """
    yield
    monkeypatch.undo()
    from kbsvc.config import reset_settings_cache

    reset_settings_cache()
    reset_engine_cache()


# ---------------------------------------------------------------------------
# 1. ensure_ready
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ensure_ready_creates_table(self, tmp_path, monkeypatch):
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        engine = _make_engine(url)
        inspector = inspect(engine)
        assert "chunk_fts" in inspector.get_table_names()
        engine.dispose()

    def test_ensure_ready_idempotent(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.ensure_ready()  # must not error

    def test_close_is_a_noop(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.close()  # must not error, must not disable further use
        store.upsert([_make_doc("c1", "hello world")])
        assert store.count() == 1


# ---------------------------------------------------------------------------
# 2. upsert + count
# ---------------------------------------------------------------------------


class TestUpsertAndCount:
    def test_upsert_and_count(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "之乎者也"), _make_doc("c2", "风马牛不相及")])
        assert store.count() == 2

    def test_upsert_replaces_existing(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "old body text", kind="section")])
        store.upsert([_make_doc("c1", "new body text", kind="table")])
        assert store.count() == 1

        flt = SearchFilter(tenant_id="t")
        hits = store.search("new", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "new body text"
        assert hits[0].payload["kind"] == "table"

    def test_upsert_empty_list_is_noop(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([])
        assert store.count() == 0


# ---------------------------------------------------------------------------
# 3. Deletes
# ---------------------------------------------------------------------------


class TestDeletes:
    def _setup_with_data(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "alpha content", document_id="d1", version_id="v1"),
                _make_doc("c2", "beta content", document_id="d1", version_id="v2"),
                _make_doc("c3", "gamma content", document_id="d2", version_id="v3"),
            ]
        )
        return store

    def test_delete_by_ids(self, tmp_path, monkeypatch):
        store = self._setup_with_data(tmp_path, monkeypatch)
        store.delete_by_ids(["c1", "c2"])
        assert store.count() == 1

    def test_delete_by_ids_empty_is_noop(self, tmp_path, monkeypatch):
        store = self._setup_with_data(tmp_path, monkeypatch)
        store.delete_by_ids([])
        assert store.count() == 3

    def test_delete_by_document(self, tmp_path, monkeypatch):
        store = self._setup_with_data(tmp_path, monkeypatch)
        store.delete_by_document("t", "d1")
        assert store.count() == 1
        flt = SearchFilter(tenant_id="t")
        hits = store.search("gamma", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].id == "c3"

    def test_delete_by_versions(self, tmp_path, monkeypatch):
        store = self._setup_with_data(tmp_path, monkeypatch)
        store.delete_by_versions("t", ["v1", "v3"])
        assert store.count() == 1
        flt = SearchFilter(tenant_id="t")
        hits = store.search("beta", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].id == "c2"

    def test_delete_by_versions_empty_is_noop(self, tmp_path, monkeypatch):
        store = self._setup_with_data(tmp_path, monkeypatch)
        store.delete_by_versions("t", [])
        assert store.count() == 3


# ---------------------------------------------------------------------------
# 4. search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_basic_term_search(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "之乎者也"), _make_doc("c2", "风马牛不相及")])

        flt = SearchFilter(tenant_id="t")
        hits = store.search("之乎", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_tenant_filter_pushed_down(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", tenant_id="t"),
                _make_doc("c2", "hello world", tenant_id="other"),
            ]
        )
        flt = SearchFilter(tenant_id="t")
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_document_id_filter_pushed_down(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", document_id="d1"),
                _make_doc("c2", "hello world", document_id="d2"),
            ]
        )
        flt = SearchFilter(tenant_id="t", document_ids=["d1"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_kind_filter_pushed_down(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", kind="section"),
                _make_doc("c2", "hello world", kind="table"),
            ]
        )
        flt = SearchFilter(tenant_id="t", kinds=["table"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c2"}

    def test_acl_filter(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", acl=["public"]),
                _make_doc("c2", "hello world", acl=["internal"]),
            ]
        )
        flt = SearchFilter(tenant_id="t", acl_any=["public"])
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_current_only_filter(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", is_current=True),
                _make_doc("c2", "hello world", is_current=False),
            ]
        )
        flt = SearchFilter(tenant_id="t", current_only=True)
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_current_only_false_returns_both(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", is_current=True),
                _make_doc("c2", "hello world", is_current=False),
            ]
        )
        flt = SearchFilter(tenant_id="t", current_only=False)
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1", "c2"}

    def test_empty_query_returns_empty(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "hello world")])
        flt = SearchFilter(tenant_id="t")
        assert store.search("", limit=10, flt=flt) == []

    def test_empty_collection_returns_empty(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        flt = SearchFilter(tenant_id="t")
        assert store.search("hello", limit=10, flt=flt) == []

    def test_payload_returned(self, tmp_path, monkeypatch):
        """The full payload dict must come back on the hit - ingest/reembed.py
        calls this 'the denormalized view both indexes carry, so a hit
        renders without a join', and the retrieval pipeline reads keys like
        'text' and 'heading_path' straight off SearchHit.payload."""
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        doc = _make_doc("c1", "贼克者取用之首法也", document_id="d7")
        doc.payload["heading_path"] = ["卷一", "总说"]
        store.upsert([doc])

        flt = SearchFilter(tenant_id="t")
        hits = store.search("贼克", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].payload["text"] == "贼克者取用之首法也"
        assert hits[0].payload["document_id"] == "d7"
        assert hits[0].payload["heading_path"] == ["卷一", "总说"]


# ---------------------------------------------------------------------------
# 5. Transactional upsert (acceptance criterion)
# ---------------------------------------------------------------------------


class TestTransactionalUpsert:
    def test_upsert_rolls_back_with_session(self, tmp_path, monkeypatch):
        """When the caller's session is rolled back, upsert data must not
        persist."""
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        engine = _make_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        session = factory()
        try:
            store.upsert([_make_doc("c1", "hello world")], session=session)
            session.rollback()
        finally:
            session.close()

        flt = SearchFilter(tenant_id="t")
        hits = store.search("hello", limit=10, flt=flt)
        assert hits == [], "upsert should have rolled back with the session"
        assert store.count() == 0

    def test_upsert_commits_with_session(self, tmp_path, monkeypatch):
        """When the caller's session is committed, upsert data persists."""
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        engine = _make_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

        session = factory()
        try:
            store.upsert([_make_doc("c1", "hello world")], session=session)
            session.commit()
        finally:
            session.close()

        flt = SearchFilter(tenant_id="t")
        hits = store.search("hello", limit=10, flt=flt)
        assert len(hits) == 1
        assert hits[0].id == "c1"

    def test_delete_by_ids_rolls_back_with_session(self, tmp_path, monkeypatch):
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "hello world")])

        engine = _make_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        session = factory()
        try:
            store.delete_by_ids(["c1"], session=session)
            session.rollback()
        finally:
            session.close()

        assert store.count() == 1, "delete should have rolled back with the session"


# ---------------------------------------------------------------------------
# 6. Filtering happens inside MATCH, not a separate SQL WHERE
# ---------------------------------------------------------------------------


class TestFilteringInsideMatch:
    def test_search_sql_has_no_separate_where_predicate(self):
        """search() must express every filter inside the MATCH argument, not
        as a separate SQL AND/WHERE predicate appended outside it. Structural
        inspection of the query text itself, not an informal read: exactly
        one WHERE clause, scoped to 'chunk_fts MATCH :query', and no residual
        'AND ...' in the surrounding SQL."""
        from kbsvc.lexical.fts5_store import _SEARCH_SQL

        sql_text = str(_SEARCH_SQL)
        assert sql_text.count("WHERE") == 1
        assert "WHERE chunk_fts MATCH :query" in sql_text
        assert " AND " not in sql_text

    def test_match_expression_embeds_every_filter(self):
        """The MATCH expression itself - not a separate WHERE - carries every
        filter field."""
        from kbsvc.lexical.fts5_store import build_match

        flt = SearchFilter(
            tenant_id="t",
            acl_any=["public"],
            document_ids=["d1"],
            source_ids=["s1"],
            kinds=["section"],
            current_only=True,
        )
        expr = build_match(["之"], flt)
        assert 'tenant_id:"t"' in expr
        assert 'is_current:"1"' in expr
        assert 'acl:("public")' in expr
        assert 'document_id:("d1")' in expr
        assert 'source_id:("s1")' in expr
        assert 'kind:("section")' in expr

    def test_explain_query_plan_is_a_single_virtual_table_scan(self, tmp_path, monkeypatch):
        """No second SCAN/SEARCH step: EXPLAIN QUERY PLAN shows exactly one
        step, the FTS5 virtual-table scan MATCH itself drives - there is no
        separate filter or join pass layered on top."""
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore, build_match

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "之乎者也")])

        flt = SearchFilter(tenant_id="t", document_ids=["d1"], acl_any=["public"])
        expr = build_match(["之"], flt)

        engine = _make_engine(url)
        with engine.connect() as conn:
            plan = conn.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT chunk_id, bm25(chunk_fts) FROM chunk_fts "
                    "WHERE chunk_fts MATCH :q ORDER BY rank LIMIT 10"
                ),
                {"q": expr},
            ).fetchall()
        engine.dispose()
        assert len(plan) == 1
        assert "VIRTUAL TABLE" in plan[0][3]


# ---------------------------------------------------------------------------
# 7. Hyphenated UUID-shaped ids must filter as an exact match (ticket concern #2)
# ---------------------------------------------------------------------------


class TestHyphenatedUuidExactMatch:
    """unicode61 splits on punctuation, including '-'. A UUID-shaped id
    indexes as several tokens, so a bareword filter would either fragment-
    match a different id sharing a token, or hit an outright FTS5 parse
    error. The toy 't'/'d1' ids used elsewhere in this suite would never
    catch this - these tests deliberately use ids that share a token."""

    def test_hyphenated_tenant_id_exact_match(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        tenant_a = "3f9e21ab-1234-4a11-9c00-abcdef012345"
        tenant_b = "3f9e21ab-9999-4a11-9c00-abcdef012345"  # shares a token with tenant_a
        store.upsert(
            [
                _make_doc("c1", "shared vocabulary here", tenant_id=tenant_a),
                _make_doc("c2", "shared vocabulary here", tenant_id=tenant_b),
            ]
        )

        flt = SearchFilter(tenant_id=tenant_a)
        hits = store.search("shared", limit=10, flt=flt)
        assert {h.id for h in hits} == {
            "c1"
        }, "hyphenated tenant_id filter leaked across tenants sharing a token"

    def test_hyphenated_document_id_exact_match(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        doc_a = "d0c0ffee-1111-4a11-9c00-abcdef012345"
        doc_b = "d0c0ffee-2222-4a11-9c00-abcdef012345"  # shares a token with doc_a
        store.upsert(
            [
                _make_doc("c1", "shared vocabulary here", document_id=doc_a),
                _make_doc("c2", "shared vocabulary here", document_id=doc_b),
            ]
        )

        flt = SearchFilter(tenant_id="t", document_ids=[doc_a])
        hits = store.search("shared", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_delete_by_document_hyphenated_id_exact_match(self, tmp_path, monkeypatch):
        """The same exact-match requirement applies to the MATCH-based bulk
        deletes, not just search()."""
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        doc_a = "3f9e21ab-1234-4a11-9c00-abcdef012345"
        doc_b = "3f9e21ab-9999-4a11-9c00-abcdef012345"
        store.upsert(
            [
                _make_doc("c1", "hello world", tenant_id="t", document_id=doc_a),
                _make_doc("c2", "hello world", tenant_id="t", document_id=doc_b),
            ]
        )

        store.delete_by_document("t", doc_a)
        assert store.count() == 1
        flt = SearchFilter(tenant_id="t")
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c2"}


# ---------------------------------------------------------------------------
# 8. MATCH string escaping (ticket concern #3)
# ---------------------------------------------------------------------------


class TestQuoteEscaping:
    def test_quote_helper_doubles_embedded_quotes(self):
        from kbsvc.lexical.fts5_store import _quote

        assert _quote('a"b') == '"a""b"'
        assert _quote("plain") == '"plain"'

    def test_embedded_quote_in_filter_value_round_trips(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        tricky_tenant = 'ten"ant'
        store.upsert([_make_doc("c1", "hello world", tenant_id=tricky_tenant)])

        flt = SearchFilter(tenant_id=tricky_tenant)
        hits = store.search("hello", limit=10, flt=flt)
        assert {h.id for h in hits} == {"c1"}

    def test_crafted_filter_value_cannot_splice_extra_match_operators(self, tmp_path, monkeypatch):
        """A document_id value crafted to look like it could close its own
        phrase and inject a second MATCH clause must not be able to - it
        must be treated as inert literal content instead."""
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "hello world", tenant_id="t", document_id="d1"),
                _make_doc("c2", "hello world", tenant_id="t", document_id="d2"),
            ]
        )

        malicious = 'd1" OR document_id:"d2'
        flt = SearchFilter(tenant_id="t", document_ids=[malicious])
        hits = store.search("hello", limit=10, flt=flt)
        assert hits == [], "a crafted document_id value must not match unrelated documents"


# ---------------------------------------------------------------------------
# 9. 五行 stays a single token (ticket acceptance criterion)
# ---------------------------------------------------------------------------


class TestWuxingSingleToken:
    """五行 must stay one term at the FTS5/unicode61 layer. Ticket 04's
    literal wording ('MATCH body:行 must not hit a row whose body only
    contains 五行') is a property of *that* layer - proven the same way
    ticket 02's test in test_sqlite_plumbing.py proves it: unicode61, given a
    contiguous CJK run with no delimiter, indexes it as one token. Re-proven
    here against ticket 04's extended DDL (+version_id, +payload) to confirm
    the column additions did not disturb it.

    Through this store, `body` is never raw text - `upsert()` always runs it
    through `analyze()` first (character unigram + bigram expansion,
    unchanged from tokenizer.py, out of scope for this ticket).
    `analyze('五行')` is literally `'五 行 五行'`: three whitespace-separated
    entries, because the unigram expansion deliberately emits 行 as its own
    recall term. Once that reaches FTS5, `MATCH 'body:"行"'` *does* hit a
    chunk whose only source text is 五行 - by design, not a bug, and a
    different property from the one this class's first test verifies (that
    FTS5 does not *additionally* re-fragment the bigram '五行' back into
    五+行 a second time, which would corrupt its term statistics rather than
    just recall broadly). The second test documents the real end-to-end
    behaviour explicitly, so nobody mistakes deliberate unigram recall for a
    regression later - mirroring the ticket's own instruction for the
    IDF-clamp divergence.
    """

    def test_raw_unicode61_keeps_wuxing_as_one_token(self, tmp_path, monkeypatch):
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        engine = _make_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO chunk_fts(chunk_id, tenant_id, document_id, version_id, "
                    "source_id, kind, acl, is_current, body, payload) VALUES "
                    "('raw1', 't', 'd1', 'v1', 's1', 'section', 'public', '1', '五行', '{}')"
                )
            )
        with engine.connect() as conn:
            hits_unigram = conn.execute(
                text(
                    "SELECT count(*) FROM chunk_fts "
                    "WHERE chunk_fts MATCH 'body:\"行\"' AND chunk_id = 'raw1'"
                )
            ).scalar()
            assert hits_unigram == 0, "raw 五行 must not fragment into a standalone 行 token"

            hits_bigram = conn.execute(
                text(
                    "SELECT count(*) FROM chunk_fts "
                    "WHERE chunk_fts MATCH 'body:\"五行\"' AND chunk_id = 'raw1'"
                )
            ).scalar()
            assert hits_bigram == 1, "raw 五行 must still match as a whole"
        engine.dispose()

    def test_end_to_end_unigram_recall_is_intentional_not_a_bug(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "五行")])

        flt = SearchFilter(tenant_id="t")
        hits = store.search("行", limit=10, flt=flt)
        assert {h.id for h in hits} == {
            "c1"
        }, "unigram recall for 行 should retrieve a 五行-only chunk by design"

        hits_bigram = store.search("五行", limit=10, flt=flt)
        assert {h.id for h in hits_bigram} == {"c1"}


# ---------------------------------------------------------------------------
# 10. bm25() sign: more-negative-is-better -> SearchHit.score higher-is-better
# ---------------------------------------------------------------------------


class TestBm25ScoreDirection:
    def test_higher_term_frequency_yields_higher_score(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert(
            [
                _make_doc("c1", "rareterm appears once here"),
                _make_doc("c2", "rareterm appears here too rareterm repeated"),
            ]
        )

        flt = SearchFilter(tenant_id="t")
        hits = store.search("rareterm", limit=10, flt=flt)
        by_id = {h.id: h.score for h in hits}
        assert by_id["c2"] > by_id["c1"], "more term occurrences should score higher"
        assert hits[0].id == "c2", "results must be ordered highest score first"


# ---------------------------------------------------------------------------
# 11. Acceptance: top-k set equality with Tantivy (df < 50%), divergence
#     asserted to exist (df >= 50%)
# ---------------------------------------------------------------------------


class TestTopKEqualityWithTantivy:
    """Same data, same query, indexed into both engines -> same top-k id set,
    for queries where every term has df < 50% (the acceptance-gate hard
    assertion). For df >= 50% queries, FTS5's bm25() clamps IDF to 1e-06 and
    Tantivy's does not (ADR-0008, measured); this is expected divergence, not
    a bug, and this class's second test asserts it exists rather than
    silently relying on it."""

    def _shared_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KB_DATABASE_URL", _make_url(tmp_path))
        monkeypatch.setenv("KB_PROFILE", "local")
        monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("KB_LEXICAL_BACKEND", "fts5")
        from kbsvc.config import reset_settings_cache

        reset_settings_cache()
        reset_engine_cache()

    def test_topk_set_equals_tantivy_below_50_percent_df(self, tmp_path, monkeypatch):
        self._shared_env(tmp_path, monkeypatch)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore
        from kbsvc.lexical.tantivy_store import TantivyLexicalStore

        # 麒麟 appears in 2 of 6 documents (df = 33% < 50%); the other four are
        # unrelated classical-poetry filler so 麒麟's own unigram/bigram terms
        # don't leak into them.
        docs = [
            _make_doc("l1", "麒麟出没于山林之间", document_id="d1"),
            _make_doc("l2", "凤凰麒麟皆为祥瑞之兽麒麟", document_id="d2"),
            _make_doc("l3", "春风又绿江南岸", document_id="d3"),
            _make_doc("l4", "明月几时有把酒问青天", document_id="d4"),
            _make_doc("l5", "大江东去浪淘尽千古风流人物", document_id="d5"),
            _make_doc("l6", "会当凌绝顶一览众山小", document_id="d6"),
        ]

        fts5 = Fts5LexicalStore()
        fts5.ensure_ready()
        fts5.upsert(docs)

        tantivy = TantivyLexicalStore()
        tantivy.ensure_ready()
        tantivy.upsert(docs)
        try:
            flt = SearchFilter(tenant_id="t")
            fts5_hits = fts5.search("麒麟", limit=6, flt=flt)
            tantivy_hits = tantivy.search("麒麟", limit=6, flt=flt)

            fts5_ids = {h.id for h in fts5_hits}
            tantivy_ids = {h.id for h in tantivy_hits}
            assert fts5_ids == tantivy_ids == {"l1", "l2"}, (
                f"fts5 top-k {fts5_ids} != tantivy top-k {tantivy_ids} for a df<50% query"
            )
        finally:
            tantivy.close()

    def test_divergence_exists_at_or_above_50_percent_df(self, tmp_path, monkeypatch):
        self._shared_env(tmp_path, monkeypatch)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore
        from kbsvc.lexical.tantivy_store import TantivyLexicalStore

        # faqiterm appears in all 4 of 4 documents (df = 100% >= 50%).
        docs = [
            _make_doc("h1", "faqiterm alpha content one", document_id="d1"),
            _make_doc("h2", "faqiterm beta content two extra words here", document_id="d2"),
            _make_doc("h3", "faqiterm gamma content three", document_id="d3"),
            _make_doc(
                "h4", "faqiterm delta content four more filler words padding", document_id="d4"
            ),
        ]

        fts5 = Fts5LexicalStore()
        fts5.ensure_ready()
        fts5.upsert(docs)

        tantivy = TantivyLexicalStore()
        tantivy.ensure_ready()
        tantivy.upsert(docs)
        try:
            flt = SearchFilter(tenant_id="t")
            fts5_hits = fts5.search("faqiterm", limit=4, flt=flt)
            tantivy_hits = tantivy.search("faqiterm", limit=4, flt=flt)
            assert len(fts5_hits) == 4
            assert len(tantivy_hits) == 4

            # This is the divergence itself, not an incidental side effect:
            # FTS5 clamps IDF to 1e-06 once df >= 50%, collapsing every score
            # toward zero regardless of term frequency; Tantivy's IDF does
            # not clamp and stays in a normal range. Measured contrast in the
            # ADR's own spike: 0.000001 vs 0.109771 (5 orders of magnitude).
            # Do not "fix" this by changing either engine's formula - ticket
            # 10 encodes it as a stratification, not a bug.
            assert all(score < 1e-3 for score in (h.score for h in fts5_hits)), (
                "fts5 scores should have collapsed near zero under the df>=50% IDF clamp"
            )
            assert all(score > 0.05 for score in (h.score for h in tantivy_hits)), (
                "tantivy scores should NOT have collapsed - its IDF does not clamp"
            )
        finally:
            tantivy.close()


# ---------------------------------------------------------------------------
# 12. bulk()
# ---------------------------------------------------------------------------


class TestBulkContextManager:
    def test_bulk_upserts_share_one_transaction_until_exit(self, tmp_path, monkeypatch):
        """Writes made without an explicit session, inside a bulk() block,
        must not become visible on a separate connection until the block
        exits - proving they share one transaction instead of each
        auto-committing (bulk()'s whole point now that there is no
        segment-merge cost to dodge)."""
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        engine = _make_engine(url)
        try:
            with store.bulk():
                store.upsert([_make_doc("c1", "hello world")])
                with engine.connect() as conn:
                    count = conn.execute(text("SELECT count(*) FROM chunk_fts")).scalar()
                assert count == 0, "bulk() write became visible before the block exited"

                store.upsert([_make_doc("c2", "more text")])
                with engine.connect() as conn:
                    count = conn.execute(text("SELECT count(*) FROM chunk_fts")).scalar()
                assert count == 0

            assert store.count() == 2
        finally:
            engine.dispose()

    def test_bulk_does_not_nest(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        with pytest.raises(KbError, match="does not nest"), store.bulk(), store.bulk():
            pass

    def test_bulk_rolls_back_all_writes_on_exception(self, tmp_path, monkeypatch):
        """If the block raises, nothing written inside it should persist -
        confirming bulk() is really one transaction."""
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        class _Boom(Exception):
            pass

        with pytest.raises(_Boom), store.bulk():
            store.upsert([_make_doc("c1", "hello world")])
            raise _Boom()

        assert store.count() == 0

    def test_explicit_session_is_not_redirected_into_the_bulk_connection(
        self, tmp_path, monkeypatch
    ):
        """A session passed explicitly to a write method must be used
        directly, not silently redirected into the open bulk connection.

        Verified at the dispatch level (`_execute_write`'s routing) with a
        sentinel object standing in for a session, rather than by holding two
        real SQLite writers open on the same file at once: that second
        scenario is a genuine single-writer conflict at the engine level
        (confirmed while building this test - it reliably deadlocks against
        `busy_timeout`) that has nothing to do with whether this store's own
        routing logic is correct, which is the property actually under test.
        """
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        seen: list[bool] = []
        with store.bulk():
            store._execute_write(lambda conn: seen.append(conn is store._bulk_conn), session=None)
            assert seen == [True], "without an explicit session, bulk's own connection must be used"

            sentinel = object()
            store._execute_write(lambda conn: seen.append(conn is sentinel), session=sentinel)
            assert seen == [True, True], (
                "an explicit session must be used directly, untouched by bulk state"
            )

    def test_explicit_session_commit_is_visible_immediately_alongside_bulk(
        self, tmp_path, monkeypatch
    ):
        """End-to-end companion to the dispatch-level test above: a session
        write committed *before* the bulk connection has written anything
        (so there is no real lock contention - see the note above) lands
        immediately, and the still-open, still-empty bulk transaction does
        not roll it back when the bulk block later exits normally."""
        url = _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()

        engine = _make_engine(url)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        try:
            with store.bulk():
                # Bulk's own connection has not written anything yet, so it
                # holds no write lock yet (SQLite's BEGIN is deferred) - an
                # independent session can commit without contention.
                session = factory()
                try:
                    store.upsert([_make_doc("c2", "session write")], session=session)
                    session.commit()
                finally:
                    session.close()

                with engine.connect() as conn:
                    count = conn.execute(
                        text("SELECT count(*) FROM chunk_fts WHERE chunk_id = 'c2'")
                    ).scalar()
                assert count == 1, "explicit-session write should be visible immediately"

                store.upsert([_make_doc("c1", "bulk write")])

            assert store.count() == 2
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# 13. recreate
# ---------------------------------------------------------------------------


class TestRecreate:
    def test_recreate_empties_all_data(self, tmp_path, monkeypatch):
        _setup_store_env(monkeypatch, tmp_path)
        from kbsvc.lexical.fts5_store import Fts5LexicalStore

        store = Fts5LexicalStore()
        store.ensure_ready()
        store.upsert([_make_doc("c1", "hello"), _make_doc("c2", "world")])
        assert store.count() == 2

        store.recreate()
        assert store.count() == 0

        # The table must still be usable afterwards.
        store.upsert([_make_doc("c3", "again")])
        assert store.count() == 1

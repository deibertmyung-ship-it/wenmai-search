"""pg_search-backed lexical index (ADR-0008 ticket 07).

**This store carries the core value of the whole ADR-0008 migration.**
`TantivyLexicalStore` takes a directory lock and a process-wide write lock
(`lexical/tantivy_store.py`'s module docstring: "one writer per process"),
capping the server profile at exactly one writing worker. pg_search embeds
Tantivy itself as a PostgreSQL index access method, so writes go through
ordinary MVCC transactions instead of a directory lock - multiple worker
processes can write concurrently (confirmed empirically against the live dev
database while building this store: two independent connections upserting
disjoint chunk ids at the same time, no errors, no lost writes, both sets
visible via the index afterward - see `tests/test_pg_search_store.py`'s
`TestConcurrentWriters`). Validating a full multi-worker deployment is ticket
08's job (it only makes sense once the publish step is one transaction);
this store only has to not be the thing that still serializes writers.

Same table-reuse architecture as `PgVectorStore` (`vector/pgvector_store.py`)
- `chunk` gains `body`/`lexical_payload` columns (`db/pg_search_ddl.py`)
rather than a separate table, so a `SearchHit` is a single-row read with no
join. `upsert` is therefore UPDATE-shaped, not INSERT-shaped, for the same
reason: `ingest/worker.py` writes the base `chunk` row
(`_persist_chunks` -> `repo.replace_chunks`) before it ever calls a store's
`upsert`, so a chunk id with no matching row is a silent no-op, not an
error - identical contract to `PgVectorStore.upsert`.

`body` is pre-analysed text, not raw
-------------------------------------
`LexicalDocument.text` (`lexical/base.py`) carries raw chunk text -
`upsert()` calls `tokenizer.analyze()` before writing it to `body`, exactly
as `TantivyLexicalStore`/`Fts5LexicalStore` do: the CJK unigram+bigram
scheme is domain knowledge that belongs in `tokenizer.py`, not re-derived by
whichever engine happens to sit underneath. The bm25 index's `body` field is
configured with a bare `whitespace` tokenizer (`db/pg_search_ddl.py`) for
exactly this reason - it must only ever see text `analyze()` already split.

This store also independently writes `source_id`/`acl`/`is_current`
--------------------------------------------------------------------
Those three columns are *shared* with `PgVectorStore` (both stores' schema-
ensure add them idempotently - see `db/pg_search_ddl.py`), but ownership of
the *data* is not implied by ownership of the *schema*. `config.py`'s
backend validator lets `KB_LEXICAL_BACKEND=pg-search` run with
`KB_VECTOR_BACKEND=sqlite-vec` - a deployment where
`PgVectorStore.upsert` never runs against `chunk` at all. If this store only
wrote `body`/`lexical_payload` on the assumption pgvector's upsert already
populated `source_id`/`acl`/`is_current` from the same payload, those three
columns would silently stay at their schema defaults (`''`/`{}`/`true`)
forever in that deployment shape, and `SearchFilter.acl_any`/`source_ids`
pushdown would quietly stop matching anything - not caught by any test that
happens to run both stores together. `upsert` therefore sets all five of
`body`/`source_id`/`acl`/`is_current`/`lexical_payload` from the same
`LexicalDocument.payload` dict every time, redundant-but-harmless when
`PgVectorStore` is also active (same source values, written twice), and the
only correct choice when it is not. `tenant_id`/`document_id`/`kind` are
NOT re-written here - those are original `Chunk` columns already set once at
`_persist_chunks` time and never change.

Filtering goes through pg_search's query DSL, never SQL WHERE
---------------------------------------------------------------
Confirmed against the live dev database (`EXPLAIN`, mirroring the ticket's
own 111.9ms-vs-429.3ms measurement): a filter expressed as
`paradedb.boolean(must => ARRAY[...])` inside the `@@@` operator compiles to
a single `with_index` Tantivy query; a filter expressed as an ordinary SQL
`AND` predicate outside `@@@` degrades to a `heap_filter` step layered on
top - `tests/test_pg_search_store.py::TestFilteringInsideDSL` asserts this
structurally via `EXPLAIN`. `build_query` mirrors
`lexical/tantivy_store.py:302-332`'s Occur.Must/Should structure: the body
terms form a real BM25 union via a single `paradedb.match('body', ...)`
call (see `_body_should`'s docstring for why - a single `match` is both
faster and score-equivalent to a `should` boolean of individual
`paradedb.term('body', ...)` clauses on this pre-analysed whitespace
corpus; `paradedb.term_set` also expresses OR/any-of semantics but,
confirmed against the real corpus, does not carry real per-term BM25
weight past a coarse match-count tier, which wrecks ranking on any query
containing a common character), tenant_id and (if `flt.current_only`)
is_current are Must, and each of `acl_any`/`source_ids`/`document_ids`/
`kinds` present becomes its own Must-wrapped `term_set` "any of these
values" clause - the same shape `tantivy_store.py`'s `_any_of` builds, and
`term_set` handles a multi-valued array column (`acl`) exactly like a
scalar column: "row matches if any of its values is in this set" (also
confirmed empirically - a chunk with `acl = {public, internal}` matches
`acl_any=["internal"]`). The distinction: those fields are exact-value
filters with no relevance to rank by, so `term_set`'s coarse scoring costs
nothing; body is free text where relevance ranking is the entire point.

Score sign: same-family engine, no negation
---------------------------------------------
`paradedb.score(id)` returns pg_search's own BM25 score, empirically
confirmed positive and higher-is-better (a two-occurrence term scored higher
than a one-occurrence term in the same corpus) - the same convention
`TantivyLexicalStore` uses (raw score, no negation), unlike
`Fts5LexicalStore`'s `-bm25(...)`. Unsurprising: the ticket's own framing is
that pg_search embeds Tantivy itself, so this is "the same engine, a
different index location" - not an independent BM25 implementation that
could have picked its own sign convention.

Concurrency
-----------
No directory lock, no process-wide write lock, no reader-reload throttle -
MVCC gives cross-transaction visibility for free the way it does for
`PgVectorStore`/`Fts5LexicalStore`. The `_lock`/`_bulk_conn` pair below is
**not** a survival of Tantivy's locking - it is the exact same narrow hazard
`Fts5LexicalStore` guards against (see that module's docstring): `bulk()`
hands out one shared `Connection` object for the life of its block, and two
threads must not drive that single object at once. `search()`/`count()`
take no lock at all. Deliberately NOT ported from `TantivyLexicalStore`:
`_commit`/`_purge`'s retry loops (Windows segment-file handle contention -
there is no directory here), `close()`'s `gc.collect()` (no mmap handles),
`_refresh_reader`/`_READER_RELOAD_INTERVAL` (MVCC gives cross-process
visibility for free), the forced single writer thread, and the process-level
write `RLock`. Reaching for any of those here would mean something had been
mis-modeled, not that a workaround was needed - Postgres's MVCC and a real
index access method do not have the problems those existed to paper over.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.pg_search_ddl import ensure_pg_search_schema
from ..db.session import get_engine
from ..errors import KbError
from .base import LexicalDocument, SearchFilter, SearchHit
from .tokenizer import analyze, tokenize

logger = logging.getLogger(__name__)


def _term_set(field: str, param: str) -> str:
    """`paradedb.term_set('field', (:param)::text[])` - "matches if any value
    in *param* is present on *field*", for both scalar columns
    (document_id/source_id/kind: "equals one of these") and the multi-valued
    `acl` array column ("overlaps this set") - confirmed empirically against
    the live dev database to be OR/any-of semantics, not AND/superset: a
    probe row carrying only one of two search terms still matched a
    `term_set` query for both terms, and a row with `acl = {internal,
    public}` matched `term_set('acl', ARRAY['internal'])` alone.

    The explicit `::text[]` cast is load-bearing, not decoration:
    `paradedb.term_set` is overloaded across every element type (`text[]`,
    `integer[]`, `boolean[]`, ...), and an untyped bind parameter is
    ambiguous between them - confirmed against the live database
    (`psycopg.errors.AmbiguousFunction: function paradedb.term_set(unknown,
    unknown) is not unique`). The same class of cast
    `pgvector_store.py::search_dense`'s `(:vector)::vector` needs, for the
    same reason: an untyped parameter has no default among several
    competing overloads. The parentheses around the bind parameter are
    equally load-bearing: SQLAlchemy's `text()` recognizes `:name` as a
    bind parameter only when it is *not* immediately followed by another
    `:` (so `foo::bar` casts are never misread as a parameter) - `:name::type`
    with no parentheses therefore fails that lookahead and the literal
    `:name::type` reaches psycopg unexpanded (`psycopg.errors.SyntaxError:
    syntax error at or near ":"`, confirmed against the live database).
    `(:name)::type` sidesteps it because `)` breaks the adjacency.

    *field* is always one of this module's own hardcoded call-site literals
    (`"acl"`/`"source_id"`/`"document_id"`/`"kind"`/`"body"`), never
    attacker- or caller-controlled data, so interpolating it directly into
    the SQL text is safe - every actual *value* still goes through a real
    bind parameter, never string concatenation.
    """
    return f"paradedb.term_set('{field}', (:{param})::text[])"


def _body_should(terms: list[str]) -> tuple[str, dict]:
    """`paradedb.match('body', :body_text, tokenizer => ...)` - "matches if
    any whitespace-separated token in *body_text* is present in body", scored
    by real per-term BM25 relevance.

    The body analyzer has already produced space-separated tokens (CJK
    unigram+bigram), so passing them to a `whitespace` tokenizer reproduces
    exactly the same term set a `should` boolean of individual
    `paradedb.term('body', :p_i)` clauses would. The single `match` call is
    much cheaper because it avoids planning and executing ~19 separate
    `paradedb.term` clauses per median query (measured ~2.1x faster on the
    real corpus, with identical top-10 sets across every query category).

    `term_set` (used for every other field in `build_query`) is deliberately
    NOT used here even though it also expresses OR/any-of semantics for a
    list of values - confirmed empirically against the real 22k-chunk
    corpus that `term_set`'s score does not carry real BM25 weight past a
    coarse "how many of the terms matched" tier. `term_set` remains the
    right choice for every other field this module builds a clause for
    (`document_id`/`source_id`/`acl`/`kind`, `tenant_id`, `is_current`) -
    those are genuinely "equals one of these values" filters with no
    relevance ranking to preserve.
    """
    params = {"body_text": " ".join(terms)}
    tokenizer_json = json.dumps({"type": "whitespace"})
    return (
        f"paradedb.match('body', :body_text, "
        f"tokenizer => '{tokenizer_json}'::jsonb)",
        params,
    )


def build_query(terms: list[str], flt: SearchFilter) -> tuple[str, dict]:
    """The whole `paradedb.boolean(must => ARRAY[...])` DSL expression for
    `search()`'s `@@@` predicate, plus its bind parameters.

    Mirrors `tantivy_store.py:302-332`'s Must/Should clause assembly -
    see the module docstring for the field-by-field correspondence. Public
    (not underscore-prefixed) so tests can inspect the expression
    structurally, mirroring `fts5_store.py::build_match`.
    """
    body_dsl, body_params = _body_should(terms)
    params: dict = {**body_params, "tenant_id": flt.tenant_id}
    clauses = [body_dsl, "paradedb.term('tenant_id', :tenant_id)"]
    if flt.current_only:
        clauses.append("paradedb.term('is_current', :is_current)")
        params["is_current"] = True
    for field, values in (
        ("acl", flt.acl_any),
        ("source_id", flt.source_ids),
        ("document_id", flt.document_ids),
        ("kind", flt.kinds),
    ):
        if values:
            param = f"{field}_values"
            clauses.append(_term_set(field, param))
            params[param] = values
    dsl = "paradedb.boolean(must => ARRAY[" + ", ".join(clauses) + "])"
    return dsl, params


def _to_row(item: LexicalDocument) -> dict:
    """One `chunk` row's lexical-side UPDATE payload, mirroring
    `tantivy_store._to_document`/`fts5_store._to_row` field for field. Only
    the columns `_UPSERT_SQL` actually sets - see the module docstring's
    "this store also independently writes source_id/acl/is_current" section
    for why those three are included despite already existing on `chunk`.
    """
    payload = item.payload
    acl = payload.get("acl") or ["public"]
    return {
        "id": item.id,
        "body": analyze(item.text),
        "source_id": str(payload.get("source_id", "")),
        "acl": [str(tag) for tag in acl],
        "is_current": bool(payload.get("is_current", True)),
        "lexical_payload": json.dumps(payload, ensure_ascii=False),
    }


_UPSERT_SQL = text(
    "UPDATE chunk SET body = :body, source_id = :source_id, acl = :acl, "
    "is_current = :is_current, lexical_payload = :lexical_payload WHERE id = :id"
)


class PgSearchLexicalStore:
    """LexicalStore backed by pg_search's bm25 index on `chunk`.

    Holds a reference to the shared engine (`get_engine()`) - no connection
    pool of its own, nothing to clean up beyond what the engine already
    owns, the same shape `PgVectorStore`/`Fts5LexicalStore` use.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: Engine = get_engine()
        self._lock = RLock()
        self._bulk_conn: Connection | None = None
        self._ready = False

    # --- lifecycle ------------------------------------------------------

    def ensure_ready(self, *, session: Session | None = None) -> None:
        """Add the lexical columns and the bm25 index to `chunk` if missing.

        Makes the store self-sufficient for callers/tests that build one
        directly against a fresh database without `init_db()` having run
        first - the same reasoning `Fts5LexicalStore.ensure_ready()` gives
        for `ensure_fts5_table`, and the same self-sufficiency property
        `SqliteVecStore.ensure_collection` gives sqlite-vec. Also covers the
        "pg-search without pgvector" deployment shape from the module
        docstring: `source_id`/`acl`/`is_current` get added here too,
        regardless of whether `PgVectorStore` ever runs in this process.

        When *session* is given (ticket 08), the DDL runs on that session's
        connection so the schema participates in the publish transaction.

        Skips the DDL entirely once this instance has already ensured it
        successfully - load-bearing, not an optimisation. See
        `PgVectorStore.ensure_collection`'s docstring for the full
        explanation: `ingest/worker.py` calls this once per chunk publish,
        `CREATE INDEX IF NOT EXISTS` on the bm25 index takes a real
        table-level lock even when it ends up doing nothing, and two
        concurrent workers both re-acquiring that lock on every publish
        deadlocks in practice (reproduced against the live dev database as
        `psycopg.errors.DeadlockDetected`). Unlike the pgvector side there is
        no runtime-known dimension to re-derive on every call, so the guard
        is a plain instance flag rather than a catalog re-check.
        """
        if self._ready:
            return
        ensure_pg_search_schema(
            session.connection() if session is not None else self._engine
        )
        self._ready = True

    def close(self) -> None:
        """No-op. The engine is shared and owned by `db.session`, same
        reasoning as `PgVectorStore.close()`/`Fts5LexicalStore.close()`."""

    # --- bulk boundary ----------------------------------------------------

    @contextmanager
    def bulk(self) -> Iterator[None]:
        """Let the caller control the commit boundary across many writes.

        Mirrors `Fts5LexicalStore.bulk()` verbatim: writes made through this
        store *without* an explicit `session` share one transaction for the
        life of the block, instead of each opening and committing its own -
        for `rebuild_lexical`'s need to keep a full-corpus rebuild from
        becoming visible half-built between batches. A `session` passed
        explicitly to a write method still always wins (see
        `_execute_write`). Unlike `TantivyLexicalStore.bulk()`, this has
        nothing to do with dodging segment-merge contention - pg_search has
        no equivalent merge pass exposed to this client - it exists solely
        for the deferred-visibility property.
        """
        with self._lock:
            if self._bulk_conn is not None:
                raise KbError("PgSearchLexicalStore.bulk() does not nest")
            with self._engine.begin() as conn:
                self._bulk_conn = conn
                try:
                    yield
                finally:
                    self._bulk_conn = None

    def recreate(self) -> None:
        """Empty the index. The migration and repair path.

        `UPDATE chunk SET body = NULL, lexical_payload = NULL` for every
        row - never `DELETE`, and never touches `source_id`/`acl`/
        `is_current`: this store does not own the canonical `chunk` row
        (identical ownership boundary to `PgVectorStore.recreate_collection`
        - see that method's docstring), only the two columns unique to it.
        `source_id`/`acl`/`is_current` are shared with `PgVectorStore`'s
        ownership too, so `recreate` leaves them alone even though this
        store also *writes* them in `upsert` - writing shared state forward
        is fine; clearing it out from under the other store on a rebuild is
        not. A NULL `body` is confirmed (against the live dev database) to
        index as simply absent - it never matches a `paradedb.term_set`
        query - so a freshly recreated index is immediately back to a clean,
        queryable-but-empty state with no separate index-drop/rebuild step.
        """
        with self._lock, self._engine.begin() as conn:
            conn.execute(text("UPDATE chunk SET body = NULL, lexical_payload = NULL"))

    # --- transaction helper ---------------------------------------------

    def _execute_write(self, fn: Callable, *, session: Session | None = None) -> None:
        """Run *fn* on a connection: the caller's session, the open `bulk()`
        connection, or (default) a short auto-committing transaction.

        Mirrors `Fts5LexicalStore._execute_write` verbatim - see that
        method's docstring for why an explicit *session* always wins
        untouched by `_lock`/`_bulk_conn` (ticket 08's transactional
        publish depends on this), and why `_lock` only ever guards the
        single shared `_bulk_conn` object, not reads.
        """
        if session is not None:
            fn(session)
            return
        with self._lock:
            if self._bulk_conn is not None:
                fn(self._bulk_conn)
            else:
                with self._engine.begin() as conn:
                    fn(conn)

    # --- writes ---------------------------------------------------------

    def upsert(self, documents: list[LexicalDocument], *, session: Session | None = None) -> None:
        """UPDATE-shaped: requires the base `chunk` row to already exist
        (written by `ingest/worker.py::_persist_chunks` before any store's
        `upsert` runs) - a chunk id with no matching row is a silent no-op,
        identical contract to `PgVectorStore.upsert`."""
        if not documents:
            return
        rows = [_to_row(doc) for doc in documents]

        def _do(conn) -> None:
            conn.execute(_UPSERT_SQL, rows)

        self._execute_write(_do, session=session)

    def delete_by_ids(self, ids: list[str], *, session: Session | None = None) -> None:
        if not ids:
            return

        def _do(conn) -> None:
            conn.execute(
                text("UPDATE chunk SET body = NULL, lexical_payload = NULL WHERE id = ANY(:ids)"),
                {"ids": ids},
            )

        self._execute_write(_do, session=session)

    def delete_by_document(
        self, tenant_id: str, document_id: str, *, session: Session | None = None
    ) -> None:
        def _do(conn) -> None:
            conn.execute(
                text(
                    "UPDATE chunk SET body = NULL, lexical_payload = NULL "
                    "WHERE tenant_id = :tenant_id AND document_id = :document_id"
                ),
                {"tenant_id": tenant_id, "document_id": document_id},
            )

        self._execute_write(_do, session=session)

    def delete_by_versions(
        self, tenant_id: str, version_ids: list[str], *, session: Session | None = None
    ) -> None:
        if not version_ids:
            return

        def _do(conn) -> None:
            conn.execute(
                text(
                    "UPDATE chunk SET body = NULL, lexical_payload = NULL "
                    "WHERE tenant_id = :tenant_id AND version_id = ANY(:version_ids)"
                ),
                {"tenant_id": tenant_id, "version_ids": version_ids},
            )

        self._execute_write(_do, session=session)

    # --- reads ----------------------------------------------------------

    def search(self, query: str, *, limit: int, flt: SearchFilter) -> list[SearchHit]:
        terms = sorted(set(tokenize(query)))
        if not terms:
            return []
        dsl, params = build_query(terms, flt)
        params["limit"] = limit
        stmt = text(
            f"SELECT id, lexical_payload, paradedb.score(id) AS score FROM chunk "
            f"WHERE id @@@ {dsl} ORDER BY score DESC LIMIT :limit"
        )

        with self._engine.connect() as conn:
            rows = conn.execute(stmt, params).fetchall()

        hits: list[SearchHit] = []
        for chunk_id, payload_raw, score in rows:
            # psycopg auto-decodes jsonb into a dict by default, but this
            # does not assume that - mirrors PgVectorStore.search_dense's
            # defensive fallback.
            payload = (
                payload_raw if isinstance(payload_raw, dict) else json.loads(payload_raw or "{}")
            )
            hits.append(SearchHit(id=chunk_id, score=float(score), payload=payload))
        return hits

    def count(self, tenant_id: str | None = None) -> int:
        """Plain SQL WHERE, not the `@@@` DSL: an unranked aggregate has no
        BM25 scoring to protect from `heap_filter` degradation, and
        `tenant_id` already has a btree index (`ix_chunk_tenant_id`) -
        mirrors `PgVectorStore.count()`'s identical reasoning and shape."""
        with self._engine.connect() as conn:
            if tenant_id:
                result = conn.execute(
                    text(
                        "SELECT count(*) FROM chunk WHERE body IS NOT NULL "
                        "AND tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                ).scalar()
            else:
                result = conn.execute(
                    text("SELECT count(*) FROM chunk WHERE body IS NOT NULL")
                ).scalar()
        return int(result or 0)

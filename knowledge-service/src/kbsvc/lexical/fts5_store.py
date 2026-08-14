"""FTS5-backed lexical index.

Lives inside the same SQLite file and engine as `chunk_vec` (ticket 03) and
the metadata tables - ADR-0008's `local` profile consolidates all three into
one `kbsvc.db`. Unlike `TantivyLexicalStore`, which owns its own commit
boundary because it is a separate process-local index with its own directory
lock, this store never commits or rolls back on its own: every write method
accepts an optional `session` and, when given one, executes on it and leaves
the transaction boundary entirely to the caller. `_execute_write` mirrors
`SqliteVecStore._execute_write` for the same reason - `chunk_fts` and
`chunk_vec` must be able to land in the caller's single transaction once
ticket 08 wires that up.

The corpus is analysed by `tokenizer.analyze` before it reaches FTS5, exactly
as it is before it reaches Tantivy: `chunk_fts` is declared with
`tokenize='unicode61'`, which only ever sees pre-analysed, whitespace-joined
text, so its own tokenizer just has to respect the whitespace boundaries
`analyze()` already put there. Do not change that split, and do not let FTS5
tokenize raw text - see `lexical/tokenizer.py` for why the analysis
(character unigrams + bigrams for CJK) is domain knowledge worth keeping
untouched.

MATCH is a query language, not a value slot
---------------------------------------------
Every `field:value` clause built here goes through `_quote`, which renders
*value* as a double-quoted FTS5 phrase literal. Two independent reasons:

1. Exact-match filtering. Only `chunk_id` is UNINDEXED; every other column
   (`tenant_id`, `document_id`, `version_id`, `source_id`, `kind`, `acl`) goes
   through `unicode61`, which splits on punctuation - including `-`. A
   UUID-shaped id like `3f9e21ab-1234-...` indexes as several separate
   tokens, so an unquoted bareword `tenant_id:3f9e21ab-1234-...` does not
   behave as an exact-match filter: it can silently match a *different*
   tenant that only shares the leading token, or fail outright with an FTS5
   parse error (confirmed while building this: `-` is a query-syntax
   operator to the parser, evaluated before tokenization ever runs, and a
   bareword hyphenated UUID raised "fts5: no such column"). Wrapping the
   value in `"..."` sends it through the same tokenizer as the indexed
   column and asks for that exact token sequence as an adjacent phrase,
   which is what "equals" actually means once fragmentation is in play.
2. Injection. The MATCH argument is itself a small boolean query language
   (AND/OR/NOT, `field:`, parens) sitting *inside* one bound SQL parameter.
   Binding the whole expression protects the outer SQL statement, but does
   nothing to stop a value containing an unescaped `"` from closing its own
   phrase early and splicing extra operators into the expression FTS5 then
   parses. Doubling an embedded `"` (FTS5's own escaping rule) closes that
   hole the same way SQL's `''` does for string literals.

`_quote` is the single place this happens; every interpolated value - filter
columns and body search terms alike - goes through it.

bm25() sign
-----------
`bm25(chunk_fts)` is more-negative-is-more-relevant. `SearchHit.score` must
be higher-is-better across every implementation - Tantivy's raw score
already is, `SqliteVecStore` does `1.0 - distance` - so `search()` negates
it.

FTS5 vs Tantivy BM25 (ADR-0008)
--------------------------------
FTS5 clamps IDF to `1e-06` once a term's document frequency reaches 50%;
Tantivy does not. Below that threshold the two engines agree to within ~1%;
at or above it FTS5's score for that term collapses close to zero while
Tantivy's does not. That is a real, measured, expected divergence - not a
bug - and it only ever affects queries built entirely from high-frequency
terms; see `tests/test_fts5_lexical_store.py::TestTopKEqualityWithTantivy`
for the acceptance-gate assertions this implies (equal top-k sets below the
threshold, divergence asserted to exist at/above it).

Concurrency
-----------
No directory lock, so - unlike Tantivy - nothing here inherently requires
single-writer discipline; SQLite's WAL mode already lets concurrent readers
proceed against the last-committed snapshot while a writer holds an open
transaction. `search()` and `count()` therefore take no lock at all, matching
`SqliteVecStore`. The one piece of shared mutable state this store has that
`SqliteVecStore` does not is `_bulk_conn` (see `bulk()`): two threads must
not both try to write through the same open connection, so the write path
(`_execute_write`, `recreate`) takes `_lock` around that specific hazard -
not, like Tantivy, around every read too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.session import ensure_fts5_table, get_engine
from ..errors import KbError
from .base import LexicalDocument, SearchFilter, SearchHit
from .tokenizer import analyze, tokenize

logger = logging.getLogger(__name__)


def _chunk_id_to_rowid(chunk_id: str) -> int:
    """Deterministic 64-bit integer from a chunk id, for use as the FTS5
    table's `rowid`.

    `chunk_id` is declared UNINDEXED (see the module docstring), so an
    equality filter on it cannot go through MATCH at all, and a plain
    `WHERE chunk_id = ?` is a full scan - FTS5's `xBestIndex` only optimises
    `MATCH`, `rowid`, and a few auxiliary constraints; confirmed against
    `EXPLAIN QUERY PLAN` while building this store (`chunk_id = ?` plans as
    `SCAN ... INDEX 0:`, no usable constraint pushed down; `rowid = ?` plans
    as `SCAN ... INDEX 0:=`). Addressing rows by a rowid derived from
    `chunk_id` turns `upsert`/`delete_by_ids` into indexed point lookups
    instead of full scans.

    Duplicated from (not imported from) `vector.sqlite_vec_store`, which
    needs the identical hash for the identical reason on the vec0 side: both
    are a four-line pure function, and importing a "private" helper across
    the vector/lexical package boundary for an incidental implementation
    detail would couple two otherwise-unrelated stores. Deterministic, so the
    same chunk_id always maps to the same rowid; collisions are negligible
    (64-bit space, far fewer than 1M chunks).
    """
    digest = hashlib.blake2b(chunk_id.encode(), digest_size=8).digest()
    return struct.unpack(">q", digest)[0]


def _quote(value: str) -> str:
    """Render *value* as an FTS5 phrase literal.

    See the module docstring ("MATCH is a query language, not a value slot")
    for why every interpolated value goes through this, with no exceptions.
    """
    return '"' + str(value).replace('"', '""') + '"'


def _match_or(field: str, values: list[str]) -> str:
    """`field:("v1" OR "v2" OR ...)` - one column, any of several values.

    *values* must be non-empty; every call site already checks that before
    calling this (an empty OR-group is not valid FTS5 syntax).
    """
    return f"{field}:(" + " OR ".join(_quote(v) for v in values) + ")"


def build_match(terms: list[str], flt: SearchFilter) -> str:
    """The whole MATCH expression for `search()`.

    One AND-chain of column-scoped clauses, so every filter is evaluated
    *inside* MATCH by FTS5's own index rather than as a separate SQL WHERE
    applied after the fact (ticket 04's filtering acceptance criterion).
    Public (not underscore-prefixed) so tests can inspect the expression
    structurally without scraping SQL text.
    """
    clauses = [_match_or("body", terms), f"tenant_id:{_quote(flt.tenant_id)}"]
    if flt.current_only:
        clauses.append(f"is_current:{_quote('1')}")
    for field, values in (
        ("acl", flt.acl_any),
        ("source_id", flt.source_ids),
        ("document_id", flt.document_ids),
        ("kind", flt.kinds),
    ):
        if values:
            clauses.append(_match_or(field, values))
    return " AND ".join(clauses)


def _to_row(item: LexicalDocument) -> dict:
    """One `chunk_fts` row, mirroring `tantivy_store._to_document` field for
    field - same source keys, same defaults - adapted for FTS5's one-value-
    per-column model: Tantivy's multi-valued `acl` field (one term per tag)
    becomes a single space-joined column here, which `unicode61` then
    tokenizes into the same per-tag tokens an `acl:(... OR ...)` clause
    matches against.
    """
    payload = item.payload
    acl = payload.get("acl") or ["public"]
    return {
        "rowid": _chunk_id_to_rowid(item.id),
        "chunk_id": item.id,
        "tenant_id": str(payload.get("tenant_id", "")),
        "document_id": str(payload.get("document_id", "")),
        "version_id": str(payload.get("version_id", "")),
        "source_id": str(payload.get("source_id", "")),
        "kind": str(payload.get("kind", "text")),
        "acl": " ".join(str(tag) for tag in acl),
        "is_current": "1" if payload.get("is_current", True) else "0",
        "body": analyze(item.text),
        "payload": json.dumps(payload, ensure_ascii=False),
    }


_UPSERT_SQL = text(
    "INSERT OR REPLACE INTO chunk_fts"
    "(rowid, chunk_id, tenant_id, document_id, version_id, source_id, kind, acl, "
    "is_current, body, payload) VALUES "
    "(:rowid, :chunk_id, :tenant_id, :document_id, :version_id, :source_id, :kind, :acl, "
    ":is_current, :body, :payload)"
)

_SEARCH_SQL = text(
    "SELECT chunk_id, bm25(chunk_fts) AS score, payload FROM chunk_fts "
    "WHERE chunk_fts MATCH :query ORDER BY rank LIMIT :limit"
)


class Fts5LexicalStore:
    """LexicalStore backed by SQLite's fts5 virtual table.

    Holds a reference to the shared engine (`get_engine()`) - no connection
    pool of its own, no directory, nothing to clean up beyond what the engine
    already owns.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: Engine = get_engine()
        self._lock = RLock()
        self._bulk_conn: Connection | None = None

    # --- lifecycle ------------------------------------------------------

    def ensure_ready(self) -> None:
        """Create `chunk_fts` if it does not exist yet.

        `init_db()` already does this for a normal process start; this makes
        the store self-sufficient for callers/tests that build one directly
        against a fresh database, the same way `SqliteVecStore.ensure_
        collection` does not depend on `init_db()` having run first.
        """
        ensure_fts5_table(self._engine)

    def close(self) -> None:
        """No-op. The engine is shared and owned by `db.session`, same
        reasoning as `SqliteVecStore.close()`."""

    # --- bulk boundary ----------------------------------------------------

    @contextmanager
    def bulk(self) -> Iterator[None]:
        """Let the caller control the commit boundary across many writes.

        Tantivy's `bulk()` existed to dodge segment-merge contention (see
        `TantivyLexicalStore.bulk`): a single deferred commit meant a single
        merge pass instead of the writer and merge threads racing over the
        same segment files. FTS5 has no segments to merge, so that reason is
        gone; what is left is `rebuild_lexical`'s need to keep a full-corpus
        rebuild from becoming visible half-built between batches. `bulk()`
        now means exactly that and nothing more: writes made through this
        store *without* an explicit `session` share one transaction for the
        life of the block, instead of each opening and committing its own. A
        `session` passed explicitly to a write method still always wins (see
        `_execute_write`), so the two never fight over who owns the commit.
        """
        with self._lock:
            if self._bulk_conn is not None:
                raise KbError("Fts5LexicalStore.bulk() does not nest")
            with self._engine.begin() as conn:
                self._bulk_conn = conn
                try:
                    yield
                finally:
                    self._bulk_conn = None

    def recreate(self) -> None:
        """Empty the index. The migration and repair path.

        Just `DELETE FROM chunk_fts` - no purge-retry dance, no
        `gc.collect()` mmap-release wait. Those exist in
        `TantivyLexicalStore` because a directory delete can race handles
        Windows has not released yet; there is no directory here, no mmap,
        nothing but a DML statement in a transaction like any other table.
        """
        with self._lock, self._engine.begin() as conn:
            conn.execute(text("DELETE FROM chunk_fts"))

    # --- transaction helper ---------------------------------------------

    def _execute_write(self, fn: Callable, *, session: Session | None = None) -> None:
        """Run *fn* on a connection: the caller's session, the open `bulk()`
        connection, or (default) a short auto-committing transaction.

        An explicit *session* always wins and is used directly, untouched by
        `_lock` or `_bulk_conn` - the caller owns that transaction outright
        (ticket 08), and this store must never redirect it into its own bulk
        connection or serialize it against one. Absent a session, `_lock`
        guards the one piece of state two threads could really collide on:
        `_bulk_conn` is a single shared connection object, and using it from
        two threads at once is unsafe even though SQLite itself would just
        serialize the underlying writes.
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
        if not documents:
            return
        rows = [_to_row(doc) for doc in documents]

        def _do(conn) -> None:
            for row in rows:
                conn.execute(_UPSERT_SQL, row)

        self._execute_write(_do, session=session)

    def delete_by_ids(self, ids: list[str], *, session: Session | None = None) -> None:
        if not ids:
            return
        rowids = [_chunk_id_to_rowid(cid) for cid in ids]

        def _do(conn) -> None:
            ph = ",".join(f":r{i}" for i in range(len(rowids)))
            params = {f"r{i}": rid for i, rid in enumerate(rowids)}
            conn.execute(text(f"DELETE FROM chunk_fts WHERE rowid IN ({ph})"), params)

        self._execute_write(_do, session=session)

    def delete_by_document(
        self, tenant_id: str, document_id: str, *, session: Session | None = None
    ) -> None:
        def _do(conn) -> None:
            match_expr = f"tenant_id:{_quote(tenant_id)} AND document_id:{_quote(document_id)}"
            conn.execute(
                text("DELETE FROM chunk_fts WHERE chunk_fts MATCH :query"),
                {"query": match_expr},
            )

        self._execute_write(_do, session=session)

    def delete_by_versions(
        self, tenant_id: str, version_ids: list[str], *, session: Session | None = None
    ) -> None:
        if not version_ids:
            return

        def _do(conn) -> None:
            match_expr = f"tenant_id:{_quote(tenant_id)} AND {_match_or('version_id', version_ids)}"
            conn.execute(
                text("DELETE FROM chunk_fts WHERE chunk_fts MATCH :query"),
                {"query": match_expr},
            )

        self._execute_write(_do, session=session)

    # --- reads ----------------------------------------------------------

    def search(self, query: str, *, limit: int, flt: SearchFilter) -> list[SearchHit]:
        terms = sorted(set(tokenize(query)))
        if not terms:
            return []
        match_expr = build_match(terms, flt)

        with self._engine.connect() as conn:
            rows = conn.execute(_SEARCH_SQL, {"query": match_expr, "limit": limit}).fetchall()

        hits: list[SearchHit] = []
        for chunk_id, score, payload_json in rows:
            payload = json.loads(payload_json) if payload_json else {}
            # bm25() is more-negative-is-better; SearchHit.score must be
            # higher-is-better across every store implementation.
            hits.append(SearchHit(id=chunk_id, score=-float(score), payload=payload))
        return hits

    def count(self, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            sql = text("SELECT count(*) FROM chunk_fts")
            params: dict = {}
        else:
            sql = text("SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH :query")
            params = {"query": f"tenant_id:{_quote(tenant_id)}"}
        with self._engine.connect() as conn:
            result = conn.execute(sql, params).scalar()
        return int(result or 0)

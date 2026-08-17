"""pgvector-backed vector store.

HNSW ANN search over PostgreSQL/ParadeDB, for the `server` profile's dense
half of retrieval. Unlike `QdrantVectorStore` and `SqliteVecStore`, this store
has no table of its own: the dense-retrieval columns (`embedding`,
`source_id`, `acl`, `is_current`, `vector_payload`) live directly on the
`chunk` table the metadata store already owns (see `db/pgvector_ddl.py`), so a
`SearchHit` is a single-row read with no join.

`upsert` is UPDATE-shaped, not INSERT-shaped. `ingest/worker.py` writes the
`chunk` row (`_persist_chunks` -> `repo.replace_chunks`) *before* it calls
`store.upsert(points)` - by the time this store sees a point, the row it
belongs to already exists. Calling `upsert` for a chunk id with no matching
row is therefore a no-op (zero rows updated), not an error: the same shape as
calling any of the delete methods for ids that are already gone.

Delete methods null out `embedding`/`vector_payload` rather than deleting the
row: this store does not own the canonical `chunk` row, and must not be able
to remove metadata other subsystems (pg_search's source data, plagiarism, the
repo layer) depend on, even if it is ever miscalled. `search_dense` filters on
`embedding IS NOT NULL`, so a nulled-out row is unsearchable, which is the
only property a delete needs to guarantee here.

Participates in caller-managed transactions the same way `SqliteVecStore`
does: write methods accept an optional `session`. When given, they execute on
it and never commit or rollback - the caller controls the transaction
boundary (ticket 08 will fold this into one transaction per publish). When
`session` is `None`, a short `engine.begin()` transaction is opened instead.

`search_dense`'s multi-valued filters (`document_ids`/`source_ids`/`kinds`
via `= ANY(...)`, `acl_any` via `&&`) force `SET LOCAL enable_indexscan =
off` - discovered empirically, not in the ticket text: the ticket's spike
only measured a *single-value scalar equality* narrow filter
(`document_id = 'doc7'`), which the default planner already handles
correctly. A 100k-row benchmark measuring a ~10% "filter by ~20 documents"
tier - the middle selectivity point the ticket explicitly asked for, since
the spike only covered 0.5% and 90% - found the default planner silently
choosing the same broken HNSW-ignores-the-filter plan the ticket's missing-
btree scenario describes, *despite* the relevant btree/GIN index existing:
Postgres's LIMIT-cost prorating assumes matching rows are spread evenly
across the HNSW scan order, which is false whenever the filter correlates
with the corpus's semantic clustering (document/source boundaries almost by
definition do). Measured at 100k x 512d: the default planner returned zero
rows for 18/20 trials on a 20-document filter; `enable_indexscan = off`
(leaving `enable_bitmapscan` on, so the btree/GIN-driven plan stays
available) returned zero rows for 0/20. See ticket 06's Comments for the
full investigation and re-measured three-tier numbers.
"""

from __future__ import annotations

import json
import logging
import weakref
from collections.abc import Callable

from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.pgvector_ddl import (
    drop_pgvector_embedding,
    ensure_pgvector_schema,
    get_pgvector_dimension,
)
from ..db.session import get_engine
from ..errors import KbError
from .base import SearchFilter, SearchHit, VectorPoint

logger = logging.getLogger(__name__)

# `search_dense` issues this - scoped to that query's own transaction via
# `SET LOCAL`, never session- or process-wide - whenever the filter carries a
# multi-valued (`= ANY(...)`) or array-overlap (`&&`) predicate. See the
# module docstring for why: without it, Postgres's planner can silently
# choose an HNSW-first plan that ignores the filter and returns too few or
# zero rows, even though the correct btree/GIN index exists and the default
# planner already uses it fine for a plain scalar-equality filter.
# `enable_bitmapscan` is deliberately left alone - disabling it too would
# force a full sequential scan (the ground-truth benchmark's methodology,
# meant for recall comparison, not for serving queries); leaving it on keeps
# the bitmap-scan-via-btree/GIN plan available, which is what the planner
# should have chosen in the first place.
_DISABLE_INDEXSCAN_SQL = text("SET LOCAL enable_indexscan = off")

# Engines that already have the pgvector psycopg adapter registered on their
# "connect" event. Keyed by the Engine object itself (not `id()`, which Python
# can reuse once an unrelated object is garbage-collected) so a second
# `PgVectorStore()` built against the same cached `get_engine()` singleton -
# the normal case, since it is `lru_cache`d - does not stack a duplicate
# listener on every construction.
_vector_adapter_registered: weakref.WeakSet[Engine] = weakref.WeakSet()


def _ensure_vector_adapter_registered(engine: Engine) -> None:
    """Register pgvector's psycopg list[float] <-> `vector` adapter.

    Without this, psycopg has no idea how to serialize a Python list for a
    `vector`-typed parameter (or deserialize one back) - the alternative is
    hand-formatting a `'[1,2,3]'` string into SQL text on every write, which
    this deliberately avoids (footgun on a hot write path: no escaping
    safety net, silent corruption on a malformed float). Registered per
    DBAPI connection via the "connect" event, the same mechanism
    `db/session.py::_load_sqlite_vec_extension` uses for sqlite-vec - each
    fresh connection needs it, not just the first one the pool opens.

    Dialect-gated, same convention `db/session.py::ensure_postgres_extensions`
    uses: no-op if *engine* is not PostgreSQL. `KB_VECTOR_BACKEND=pgvector`
    requires a PostgreSQL `database_url` (`config.py`'s
    `_backends_are_reachable_from_this_deployment` validator), so this should
    never see anything else in a correctly configured deployment - but
    `get_engine()` is a process-wide `lru_cache`d singleton, and a test that
    swaps `KB_VECTOR_BACKEND` without also resetting the engine cache (a
    pre-existing gap in `tests/test_store_factories.py`'s `select_backend`
    fixture, not this store's to fix) can hand `PgVectorStore.__init__` a
    stale SQLite engine. Without this guard, the "connect" listener below
    fires on the *next* real SQLite connection and crashes with `TypeError:
    expected Connection or AsyncConnection, got Connection` - psycopg's
    `register_vector` handed a `sqlite3.Connection` - confirmed by running
    the full suite: `tests/test_store_factories.py`'s own pgvector-backend
    tests are what trigger it. Construction must stay inert regardless of
    what `get_engine()` happens to return; real dialect mismatches still
    fail loudly and correctly at actual use (`ensure_collection`,
    `search_dense`, ...), which is where they belong.
    """
    if engine.dialect.name != "postgresql":
        return
    if engine in _vector_adapter_registered:
        return
    _vector_adapter_registered.add(engine)

    @event.listens_for(engine, "connect")
    def _register_vector_type(dbapi_conn, _record):  # pragma: no cover - driver hook
        from pgvector.psycopg import register_vector

        register_vector(dbapi_conn)


class PgVectorStore:
    """VectorStore backed by pgvector's HNSW index on `chunk.embedding`.

    Holds a reference to the shared engine (`get_engine()`) and a cached
    dimension, the same shape `SqliteVecStore` uses - stateless beyond that,
    no connection pool of its own, no directory to clean up.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: Engine = get_engine()
        self._dim: int | None = None
        _ensure_vector_adapter_registered(self._engine)

    # --- lifecycle ------------------------------------------------------

    def ensure_collection(self, dim: int) -> None:
        """Add the dense-retrieval columns/indexes to `chunk` if missing.

        If `embedding` already exists with a different dimension, raise so
        the caller can decide to `recreate_collection` instead of silently
        writing vectors of the wrong width.
        """
        existing = get_pgvector_dimension(self._engine)
        if existing is not None and existing != dim:
            raise KbError(
                "chunk.embedding already exists with a different embedding dimension",
                {"existing": existing, "requested": dim},
            )
        ensure_pgvector_schema(self._engine, dim=dim)
        self._dim = dim

    def recreate_collection(self, dim: int) -> None:
        """Drop and rebuild the `embedding` column and its HNSW index.

        Required on model/dimension change. Unlike `QdrantVectorStore` and
        `SqliteVecStore`, this never drops `chunk` itself - see
        `db/pgvector_ddl.py::drop_pgvector_embedding` for why. Existing rows
        lose their vectors (expected: a dimension change means re-embedding
        everyone anyway) but keep their metadata.
        """
        drop_pgvector_embedding(self._engine)
        self._dim = None
        self.ensure_collection(dim)

    def close(self) -> None:
        """No-op. The engine is shared and owned by `db.session`."""

    # --- transaction helper ---------------------------------------------

    def _execute_write(
        self,
        fn: Callable,
        *,
        session: Session | None = None,
    ) -> None:
        """Run *fn* on a connection, either the caller's session or a new
        auto-commit transaction from the engine."""
        if session is not None:
            fn(session)
        else:
            with self._engine.begin() as conn:
                fn(conn)

    # --- writes ---------------------------------------------------------

    def upsert(
        self,
        points: list[VectorPoint],
        *,
        session: Session | None = None,
    ) -> None:
        if not points:
            return
        if self._dim is None:
            raise KbError("ensure_collection must be called before upsert")

        rows = []
        for point in points:
            p = point.payload
            rows.append(
                {
                    "id": point.id,
                    "embedding": point.dense,
                    "source_id": p.get("source_id", ""),
                    "acl": p.get("acl") or ["public"],
                    "is_current": bool(p.get("is_current", True)),
                    "vector_payload": json.dumps(p, ensure_ascii=False),
                }
            )

        self._execute_write(lambda conn: self._upsert_rows(conn, rows), session=session)

    def _upsert_rows(self, conn, rows: list[dict]) -> None:
        """UPDATE, not INSERT - see the module docstring. Executed as one
        `executemany`-style call (a list of parameter dicts) rather than a
        per-row Python loop, so the driver can batch the round trips."""
        stmt = text(
            "UPDATE chunk SET embedding = :embedding, source_id = :source_id, "
            "acl = :acl, is_current = :is_current, vector_payload = :vector_payload "
            "WHERE id = :id"
        )
        conn.execute(stmt, rows)

    def delete_by_ids(
        self,
        ids: list[str],
        *,
        session: Session | None = None,
    ) -> None:
        if not ids:
            return

        def _do(conn) -> None:
            conn.execute(
                text(
                    "UPDATE chunk SET embedding = NULL, vector_payload = NULL "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            )

        self._execute_write(_do, session=session)

    def delete_by_document(
        self,
        tenant_id: str,
        document_id: str,
        *,
        session: Session | None = None,
    ) -> None:
        def _do(conn) -> None:
            conn.execute(
                text(
                    "UPDATE chunk SET embedding = NULL, vector_payload = NULL "
                    "WHERE tenant_id = :tenant_id AND document_id = :document_id"
                ),
                {"tenant_id": tenant_id, "document_id": document_id},
            )

        self._execute_write(_do, session=session)

    def delete_by_versions(
        self,
        tenant_id: str,
        version_ids: list[str],
        *,
        session: Session | None = None,
    ) -> None:
        if not version_ids:
            return

        def _do(conn) -> None:
            conn.execute(
                text(
                    "UPDATE chunk SET embedding = NULL, vector_payload = NULL "
                    "WHERE tenant_id = :tenant_id AND version_id = ANY(:version_ids)"
                ),
                {"tenant_id": tenant_id, "version_ids": version_ids},
            )

        self._execute_write(_do, session=session)

    # --- reads ------------------------------------------------------------

    def search_dense(
        self,
        vector: list[float],
        *,
        limit: int,
        flt: SearchFilter,
    ) -> list[SearchHit]:
        if self._dim is None:
            return []

        # `embedding IS NOT NULL` is technically implied by the HNSW index
        # (it never contains NULL entries), but it is spelled out anyway: a
        # deleted-but-not-purged row (see the module docstring) must never
        # come back from a search regardless of how the planner gets there.
        conditions = ["embedding IS NOT NULL", "tenant_id = :tenant_id"]
        params: dict = {"tenant_id": flt.tenant_id, "vector": vector, "limit": limit}

        if flt.current_only:
            conditions.append("is_current = true")
        # `needs_exact_scan` tracks whether the planner override below is
        # required - see `_DISABLE_INDEXSCAN_SQL` for the underlying bug.
        # Measured thresholds (100k x 512d, live dev database):
        #   - a single-valued `document_id/source_id = ANY(:x)` (one-element
        #     list) is exactly as safe as a plain scalar `=` - the default
        #     planner already picks the correct btree-driven plan (0/15
        #     zero-row trials either way) - so a length check keeps the
        #     ticket's own primary "one narrow document" scenario on the
        #     fast path instead of paying the override's cost for nothing.
        #   - two or more values flips the planner's choice reliably (15/15
        #     and 14/15 zero-row trials at 2 and 20 elements respectively).
        #   - `acl_any` goes through GIN `&&`, not btree `= ANY`, and proved
        #     unsafe even at a single tag (7/15 zero-row trials filtering
        #     for the ~10%-selectivity tag alone) - always forced, length
        #     is not a safe signal for this operator.
        needs_exact_scan = False
        if flt.acl_any:
            # Array-overlap, pushed down - see db/pgvector_ddl.py's docstring
            # on why `acl` is `text[]` + GIN rather than scalar + btree.
            conditions.append("acl && :acl_any")
            params["acl_any"] = flt.acl_any
            needs_exact_scan = True
        if flt.source_ids:
            conditions.append("source_id = ANY(:source_ids)")
            params["source_ids"] = flt.source_ids
            needs_exact_scan = needs_exact_scan or len(flt.source_ids) > 1
        if flt.document_ids:
            conditions.append("document_id = ANY(:document_ids)")
            params["document_ids"] = flt.document_ids
            needs_exact_scan = needs_exact_scan or len(flt.document_ids) > 1
        if flt.kinds:
            conditions.append("kind = ANY(:kinds)")
            params["kinds"] = flt.kinds
            needs_exact_scan = needs_exact_scan or len(flt.kinds) > 1

        where = " AND ".join(conditions)
        # `distance` is referenced by its SELECT-list alias in ORDER BY
        # rather than repeating `embedding <=> :vector` a second time - same
        # value, computed once, and the standard pgvector idiom for keeping
        # the planner's KNN-via-HNSW recognition intact.
        #
        # `(:vector)::vector` - both the parens and the cast are load-bearing.
        # Unlike a direct `UPDATE ... SET embedding = :embedding` (where
        # Postgres infers the parameter's type from the assignment target),
        # a bare comparison operand has no such target to infer from, so
        # without the cast psycopg defaults an unadorned Python list to
        # `double precision[]` and Postgres rejects `vector <=> double
        # precision[]` outright (confirmed against the live dev database).
        # The parens are equally load-bearing from the other direction:
        # SQLAlchemy's `text()` bindparam scanner does not recognize
        # `:vector::vector` as a parameter followed by a cast - it silently
        # fails to substitute it at all, `(:vector)::vector` is the form
        # that reads correctly on both sides.
        stmt = text(
            f"SELECT id, vector_payload, embedding <=> (:vector)::vector AS distance "
            f"FROM chunk WHERE {where} ORDER BY distance LIMIT :limit"
        )

        with self._engine.begin() as conn:
            if needs_exact_scan:
                conn.execute(_DISABLE_INDEXSCAN_SQL)
            rows = conn.execute(stmt, params).fetchall()

        hits: list[SearchHit] = []
        for chunk_id, payload_raw, distance in rows:
            # psycopg auto-decodes jsonb into a dict by default, but this
            # does not assume that - a defensive `json.loads` fallback costs
            # nothing and keeps the store correct if that ever changes.
            payload = (
                payload_raw if isinstance(payload_raw, dict) else json.loads(payload_raw or "{}")
            )
            # pgvector's `<=>` is cosine *distance* (1 - cosine_similarity).
            # Converting to a higher-is-better score matches SqliteVecStore
            # and QdrantVectorStore's Distance.COSINE convention.
            score = 1.0 - float(distance)
            hits.append(SearchHit(id=chunk_id, score=score, payload=payload))
        return hits

    def count(self, tenant_id: str | None = None) -> int:
        with self._engine.connect() as conn:
            if tenant_id:
                result = conn.execute(
                    text(
                        "SELECT count(*) FROM chunk WHERE embedding IS NOT NULL "
                        "AND tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                ).scalar()
            else:
                result = conn.execute(
                    text("SELECT count(*) FROM chunk WHERE embedding IS NOT NULL")
                ).scalar()
        return int(result or 0)

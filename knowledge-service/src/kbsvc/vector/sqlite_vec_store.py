"""sqlite-vec-backed vector store.

Exact brute-force KNN over a single SQLite file.  No ANN index, no directory
lock, no separate process.  Designed for the ``local`` profile where the whole
corpus fits in one ``kbsvc.db`` alongside the metadata tables and the FTS5
lexical index.

The store participates in caller-managed transactions: write methods accept an
optional ``session`` parameter.  When provided, they execute on that session's
connection and never commit or rollback themselves - the caller controls the
transaction boundary.  When ``session`` is ``None``, they open a short
transaction via ``engine.begin()``.

Read methods (``search_dense``, ``count``) always use their own connection
because they never need to participate in a write transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from collections.abc import Callable

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db.session import get_engine
from ..db.vec_ddl import (
    drop_vec0_table,
    ensure_vec0_table,
    get_vec0_dimension,
)
from ..errors import KbError
from .base import SearchFilter, SearchHit, VectorPoint

logger = logging.getLogger(__name__)


def _chunk_id_to_rowid(chunk_id: str) -> int:
    """Deterministic 64-bit integer from a chunk id for use as vec0 rowid.

    vec0 requires an integer rowid; chunk ids are UUID strings.  The hash is
    deterministic so the same chunk_id always maps to the same rowid, and
    collisions are negligible (64-bit space, < 1M chunks).
    """
    digest = hashlib.blake2b(chunk_id.encode(), digest_size=8).digest()
    return struct.unpack(">q", digest)[0]


def _vector_to_blob(vector: list[float]) -> bytes:
    """Pack a float vector into the little-endian bytes sqlite-vec expects."""
    return struct.pack(f"<{len(vector)}f", *vector)


class SqliteVecStore:
    """VectorStore backed by sqlite-vec's vec0 virtual table.

    The store holds a reference to the shared engine (``get_engine()``) and a
    cached dimension.  It is stateless beyond that - no connection pool, no
    background threads, no directory to clean up.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engine: Engine = get_engine()
        self._dim: int | None = None

    # --- lifecycle ------------------------------------------------------

    def ensure_collection(
        self, dim: int, *, session: Session | None = None
    ) -> None:
        """Create the vec0 table if it does not exist.

        If the table already exists with a different dimension, raise so the
        caller can decide to ``recreate_collection`` instead of silently
        writing bad data.

        When *session* is given (ticket 08's transactional publish), the
        dimension check and DDL run on that session's connection - opening a
        second connection to ``CREATE VIRTUAL TABLE`` while the caller already
        holds SQLite's write lock deadlocks. With ``session=None`` a short
        standalone transaction is used, as before.
        """
        bind = session.connection() if session is not None else self._engine
        existing = get_vec0_dimension(bind)
        if existing is not None and existing != dim:
            raise KbError(
                "chunk_vec already exists with a different embedding dimension",
                {"existing": existing, "requested": dim},
            )
        ensure_vec0_table(bind, dim=dim)
        self._dim = dim

    def recreate_collection(
        self, dim: int, *, session: Session | None = None
    ) -> None:
        """Drop and rebuild the vec0 table.  Required on model / dimension change."""
        bind = session.connection() if session is not None else self._engine
        drop_vec0_table(bind)
        self._dim = None
        self.ensure_collection(dim, session=session)

    def close(self) -> None:
        """No-op.  The engine is shared and owned by ``db.session``."""

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
            rowid = _chunk_id_to_rowid(point.id)
            vec_blob = _vector_to_blob(point.dense)
            p = point.payload
            rows.append({
                "rowid": rowid,
                "embedding": vec_blob,
                "chunk_id": point.id,
                "document_id": p.get("document_id", ""),
                "version_id": p.get("version_id", ""),
                "source_id": p.get("source_id", ""),
                "kind": p.get("kind", ""),
                "is_current": 1 if p.get("is_current", True) else 0,
                "tenant": p.get("tenant_id", ""),
                "payload": json.dumps(p, ensure_ascii=False),
            })

        self._execute_write(lambda conn: self._upsert_rows(conn, rows), session=session)

    def _upsert_rows(self, conn, rows: list[dict]) -> None:
        """Execute upsert SQL on *conn* (a Session or Connection).

        vec0 does not support ``INSERT OR REPLACE``, so each upsert is a
        DELETE + INSERT pair.  Both run on the same connection/transaction.
        Deletes are batched via ``executemany``; inserts are per-row because
        vec0's virtual table interface does not reliably support
        ``executemany`` for writes.
        """
        del_vec_sql = text("DELETE FROM chunk_vec WHERE rowid = :rowid")
        del_payload_sql = text(
            "DELETE FROM chunk_vec_payload WHERE chunk_id = :chunk_id"
        )
        vec_sql = text(
            "INSERT INTO chunk_vec"
            "(rowid, embedding, chunk_id, document_id, version_id, "
            "source_id, kind, is_current, tenant) "
            "VALUES (:rowid, :embedding, :chunk_id, :document_id, "
            ":version_id, :source_id, :kind, :is_current, :tenant)"
        )
        payload_sql = text(
            "INSERT INTO chunk_vec_payload(chunk_id, payload) "
            "VALUES (:chunk_id, :payload)"
        )
        # Batch-delete existing rows (if any) before inserting.
        conn.execute(del_vec_sql, [{"rowid": r["rowid"]} for r in rows])
        conn.execute(
            del_payload_sql, [{"chunk_id": r["chunk_id"]} for r in rows]
        )
        for row in rows:
            conn.execute(vec_sql, row)
            conn.execute(payload_sql, {"chunk_id": row["chunk_id"], "payload": row["payload"]})

    def delete_by_ids(
        self,
        ids: list[str],
        *,
        session: Session | None = None,
    ) -> None:
        if not ids:
            return

        def _do(conn) -> None:
            ph = ",".join(":c" + str(i) for i in range(len(ids)))
            params = {f"c{i}": cid for i, cid in enumerate(ids)}
            conn.execute(
                text(f"DELETE FROM chunk_vec WHERE chunk_id IN ({ph})"), params
            )
            conn.execute(
                text(f"DELETE FROM chunk_vec_payload WHERE chunk_id IN ({ph})"),
                params,
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
            # Find chunk_ids before deleting vec0 rows, so the payload table
            # can be cleaned up in the same transaction.
            ids = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT chunk_id FROM chunk_vec "
                        "WHERE tenant = :t AND document_id = :d"
                    ),
                    {"t": tenant_id, "d": document_id},
                )
            ]
            if not ids:
                return
            conn.execute(
                text(
                    "DELETE FROM chunk_vec WHERE tenant = :t AND document_id = :d"
                ),
                {"t": tenant_id, "d": document_id},
            )
            ph = ",".join(":c" + str(i) for i in range(len(ids)))
            params = {f"c{i}": cid for i, cid in enumerate(ids)}
            conn.execute(
                text(f"DELETE FROM chunk_vec_payload WHERE chunk_id IN ({ph})"),
                params,
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
            ph = ",".join(":v" + str(i) for i in range(len(version_ids)))
            params: dict = {"t": tenant_id}
            params.update({f"v{i}": vid for i, vid in enumerate(version_ids)})

            # Find chunk_ids before deleting vec0 rows.
            ids = [
                row[0]
                for row in conn.execute(
                    text(
                        f"SELECT chunk_id FROM chunk_vec "
                        f"WHERE tenant = :t AND version_id IN ({ph})"
                    ),
                    params,
                )
            ]
            if not ids:
                return
            conn.execute(
                text(
                    f"DELETE FROM chunk_vec "
                    f"WHERE tenant = :t AND version_id IN ({ph})"
                ),
                params,
            )
            cph = ",".join(":c" + str(i) for i in range(len(ids)))
            cparams = {f"c{i}": cid for i, cid in enumerate(ids)}
            conn.execute(
                text(f"DELETE FROM chunk_vec_payload WHERE chunk_id IN ({cph})"),
                cparams,
            )

        self._execute_write(_do, session=session)

    # --- reads ----------------------------------------------------------

    def search_dense(
        self,
        vector: list[float],
        *,
        limit: int,
        flt: SearchFilter,
    ) -> list[SearchHit]:
        if self._dim is None:
            # Resolve from `sqlite_master` rather than trusting instance
            # state - see `PgVectorStore.search_dense` for the full story.
            # Same defect, and it reached production there: `_dim` records a
            # fact about the database but only `ensure_collection` writes it,
            # so a read-only process (`kbsvc search`, which goes straight from
            # `init_db()` to the retrieval pipeline) silently reported an
            # empty dense half instead of querying.
            self._dim = get_vec0_dimension(self._engine)
            if self._dim is None:
                return []  # `chunk_vec` genuinely does not exist yet
        vec_blob = _vector_to_blob(vector)

        # Build the KNN query with push-down filters.
        # vec0's KNN syntax: WHERE embedding MATCH ? AND k = ?
        # Additional metadata column filters go in the same WHERE clause.
        # vec0 already returns results ordered by distance, so no explicit
        # ORDER BY is needed (adding one forces a redundant sort).
        conditions = ["embedding MATCH :vec", "k = :k", "tenant = :tenant"]
        params: dict = {"vec": vec_blob, "k": limit, "tenant": flt.tenant_id}

        if flt.document_ids:
            ph = ",".join(":d" + str(i) for i in range(len(flt.document_ids)))
            conditions.append(f"document_id IN ({ph})")
            params.update({f"d{i}": d for i, d in enumerate(flt.document_ids)})

        if flt.source_ids:
            ph = ",".join(":s" + str(i) for i in range(len(flt.source_ids)))
            conditions.append(f"source_id IN ({ph})")
            params.update({f"s{i}": s for i, s in enumerate(flt.source_ids)})

        if flt.kinds:
            ph = ",".join(":k" + str(i) for i in range(len(flt.kinds)))
            conditions.append(f"kind IN ({ph})")
            params.update({f"k{i}": k for i, k in enumerate(flt.kinds)})

        if flt.current_only:
            conditions.append("is_current = 1")

        where = " AND ".join(conditions)
        knn_sql = text(
            f"SELECT chunk_id, distance FROM chunk_vec WHERE {where}"
        )

        # Use a single connection for both the KNN and payload fetch.
        # Opening a second connection would reload the sqlite-vec extension
        # and add ~100 ms of overhead on the 100k benchmark.
        with self._engine.connect() as conn:
            results = conn.execute(knn_sql, params).fetchall()
            if not results:
                return []

            # Fetch payloads on the same connection.
            chunk_ids = [r[0] for r in results]
            ph = ",".join(":c" + str(i) for i in range(len(chunk_ids)))
            payload_params = {f"c{i}": cid for i, cid in enumerate(chunk_ids)}
            payload_rows = conn.execute(
                text(
                    f"SELECT chunk_id, payload FROM chunk_vec_payload "
                    f"WHERE chunk_id IN ({ph})"
                ),
                payload_params,
            ).fetchall()

        payload_map = {row[0]: json.loads(row[1]) for row in payload_rows}

        # Post-filter by ACL (list intersection test, can't push down to vec0).
        hits: list[SearchHit] = []
        for chunk_id, distance in results:
            payload = payload_map.get(chunk_id, {})
            if flt.acl_any:
                doc_acl = set(payload.get("acl", ["public"]))
                if not doc_acl.intersection(flt.acl_any):
                    continue
            # Convert distance (lower = better) to score (higher = better).
            # cosine distance is 1 - cosine_similarity, so score = 1 - distance.
            score = 1.0 - float(distance)
            hits.append(SearchHit(id=chunk_id, score=score, payload=payload))

        return hits

    def count(self, tenant_id: str | None = None) -> int:
        with self._engine.connect() as conn:
            # The vec0 table is created lazily on the first publish, inside that
            # publish's transaction (ticket 08). If no publish has ever
            # committed - a fresh database, or one whose first publish rolled
            # back - the table does not exist yet. That is an empty index, not
            # an error: report 0 instead of "no such table".
            exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_vec'")
            ).first()
            if exists is None:
                return 0
            if tenant_id:
                result = conn.execute(
                    text("SELECT count(*) FROM chunk_vec WHERE tenant = :t"),
                    {"t": tenant_id},
                ).scalar()
            else:
                result = conn.execute(text("SELECT count(*) FROM chunk_vec")).scalar()
        return int(result or 0)

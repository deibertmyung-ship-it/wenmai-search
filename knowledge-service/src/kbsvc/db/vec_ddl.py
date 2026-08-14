"""DDL helpers for SQLite virtual tables (ADR-0008).

The vec0 virtual table needs a runtime-discovered embedding dimension, so it
cannot be created in ``init_db`` alongside the metadata tables.  Instead,
``ensure_vec0_table`` is called from ``ensure_collection(dim)`` on the store
that owns the vector half of retrieval.

FTS5 lives in ``init_db`` because it has no dimension dependency.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from .session import get_engine

_VEC0_DDL_TEMPLATE = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0("
    "embedding float[{dim}], "
    "chunk_id text, "
    "document_id text, "
    "version_id text, "
    "source_id text, "
    "kind text, "
    "is_current integer, "
    "tenant text partition key"
    ")"
)

# Regular table for the full JSON payload.  vec0 metadata columns are for
# filter push-down only; the denormalized view that a SearchHit carries is too
# large and too variable to spread across vec0 columns.  Kept in the same
# SQLite file so a single transaction covers both writes (ticket 08).
_PAYLOAD_DDL = (
    "CREATE TABLE IF NOT EXISTS chunk_vec_payload ("
    "chunk_id TEXT PRIMARY KEY, "
    "payload TEXT NOT NULL"
    ") WITHOUT ROWID"
)


def ensure_vec0_table(engine: Engine | None = None, *, dim: int) -> None:
    """Create the ``chunk_vec`` vec0 virtual table and ``chunk_vec_payload``
    if they do not exist.

    Idempotent via ``IF NOT EXISTS``.  The *dim* parameter is the embedding
    dimension discovered at runtime from the dense embedder.
    """
    if engine is None:
        engine = get_engine()
    ddl = _VEC0_DDL_TEMPLATE.format(dim=dim)
    with engine.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text(_PAYLOAD_DDL))


def drop_vec0_table(engine: Engine | None = None) -> None:
    """Drop the ``chunk_vec`` and ``chunk_vec_payload`` tables.

    Used by ``recreate_collection``.
    """
    if engine is None:
        engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS chunk_vec"))
        conn.execute(text("DROP TABLE IF EXISTS chunk_vec_payload"))


def get_vec0_dimension(engine: Engine | None = None) -> int | None:
    """Return the embedding dimension of an existing ``chunk_vec`` table.

    Returns ``None`` if the table does not exist.  Parses the ``embedding``
    column type from ``sqlite_master`` because vec0 virtual tables are not
    visible to ``PRAGMA table_info``.
    """
    import re

    if engine is None:
        engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vec'")
        ).first()
    if row is None:
        return None
    match = re.search(r"embedding\s+float\[(\d+)\]", row[0])
    return int(match.group(1)) if match else None

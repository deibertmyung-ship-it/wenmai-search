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
    "document_id text, "
    "tenant text partition key"
    ")"
)


def ensure_vec0_table(engine: Engine | None = None, *, dim: int) -> None:
    """Create the ``chunk_vec`` vec0 virtual table if it does not exist.

    Idempotent via ``IF NOT EXISTS``.  The *dim* parameter is the embedding
    dimension discovered at runtime from the dense embedder.
    """
    if engine is None:
        engine = get_engine()
    ddl = _VEC0_DDL_TEMPLATE.format(dim=dim)
    with engine.begin() as conn:
        conn.execute(text(ddl))


def drop_vec0_table(engine: Engine | None = None) -> None:
    """Drop the ``chunk_vec`` table.  Used by ``recreate_collection``."""
    if engine is None:
        engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS chunk_vec"))

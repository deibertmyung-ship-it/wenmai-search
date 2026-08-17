"""DDL helpers for SQLite virtual tables (ADR-0008).

The vec0 virtual table needs a runtime-discovered embedding dimension, so it
cannot be created in ``init_db`` alongside the metadata tables.  Instead,
``ensure_vec0_table`` is called from ``ensure_collection(dim)`` on the store
that owns the vector half of retrieval.

FTS5 lives in ``init_db`` because it has no dimension dependency.
"""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text

from .session import get_engine

Bind = Engine | Connection


@contextmanager
def _begin(bind: Bind):
    """Run DDL on *bind*: if it is a live Connection, use it as-is (the caller
    owns the transaction - ticket 08's publish needs the vec0 table created
    inside the worker's existing transaction, otherwise SQLite sees a second
    connection blocking on the write lock the first already holds); if it is an
    Engine, open a short transaction the way this module always did."""
    if isinstance(bind, Connection):
        yield bind
    else:
        with bind.begin() as conn:
            yield conn


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


def ensure_vec0_table(
    bind: Bind | None = None, *, dim: int
) -> None:
    """Create the ``chunk_vec`` vec0 virtual table and ``chunk_vec_payload``
    if they do not exist.

    Idempotent via ``IF NOT EXISTS``.  The *dim* parameter is the embedding
    dimension discovered at runtime from the dense embedder. *bind* may be a
    live ``Connection`` (the caller's transaction, used by ticket 08's
    transactional publish) or an ``Engine`` (a short standalone transaction).
    """
    if bind is None:
        bind = get_engine()
    ddl = _VEC0_DDL_TEMPLATE.format(dim=dim)
    with _begin(bind) as conn:
        conn.execute(text(ddl))
        conn.execute(text(_PAYLOAD_DDL))


def drop_vec0_table(bind: Bind | None = None) -> None:
    """Drop the ``chunk_vec`` and ``chunk_vec_payload`` tables.

    Used by ``recreate_collection``.
    """
    if bind is None:
        bind = get_engine()
    with _begin(bind) as conn:
        conn.execute(text("DROP TABLE IF EXISTS chunk_vec"))
        conn.execute(text("DROP TABLE IF EXISTS chunk_vec_payload"))


def get_vec0_dimension(bind: Bind | None = None) -> int | None:
    """Return the embedding dimension of an existing ``chunk_vec`` table.

    Returns ``None`` if the table does not exist.  Parses the ``embedding``
    column type from ``sqlite_master`` because vec0 virtual tables are not
    visible to ``PRAGMA table_info``.
    """
    import re

    if bind is None:
        bind = get_engine()
    if isinstance(bind, Connection):
        row = bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vec'")
        ).first()
    else:
        with bind.connect() as conn:
            row = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vec'")
            ).first()
    if row is None:
        return None
    match = re.search(r"embedding\s+float\[(\d+)\]", row[0])
    return int(match.group(1)) if match else None

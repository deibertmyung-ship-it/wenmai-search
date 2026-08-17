"""DDL helpers for pgvector-backed dense retrieval (ADR-0008 ticket 06).

Unlike sqlite-vec's ``chunk_vec`` (a separate virtual table, see
``db/vec_ddl.py``), pgvector has no reason to avoid the metadata store: the
`server` profile's `chunk` table already lives in the same PostgreSQL
database, so the dense-retrieval columns are added directly onto it via
additive ``ALTER TABLE`` statements. This keeps a `SearchHit` a single-row
read with no join, the same property `chunk_vec_payload` gives sqlite-vec.

Like ``vec0``'s embedding dimension, the `vector(N)` width is only known at
runtime (from the dense embedder), so this cannot run inside ``init_db()``
alongside the metadata tables - it is called from
``PgVectorStore.ensure_collection(dim)`` instead, mirroring
``ensure_vec0_table``.

Deliberately not folded into ``db/session.py``'s ``_ADDITIVE_COLUMNS``: that
tuple is applied unconditionally to every dialect including SQLite (no
dialect gate in ``_apply_additive_columns``), so a `vector(N)` or `text[]`
type in there would break `init_db()` on the local profile the moment this
module is imported. Everything here is invoked lazily by `PgVectorStore` and
never by `init_db()`.
"""

from __future__ import annotations

import re

from sqlalchemy import Engine, text

from .session import get_engine

# The embedding column's width is a template parameter (runtime-discovered);
# every other statement here is static and shared across all dimensions.
_ADD_EMBEDDING_COLUMN_SQL = "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS embedding vector({dim})"

# `source_id`, `acl`, `is_current`, `vector_payload` mirror the fields
# `QdrantVectorStore` carries as payload keys and `chunk_vec` carries as
# metadata columns (see `vector/qdrant_store.py`'s `_PAYLOAD_KEYWORD_INDEXES`
# and `db/vec_ddl.py`). `chunk` has no columns for them yet (verified against
# a live ParadeDB instance) - `source_id`/`acl`/`is_current` today live only
# inside the Qdrant/vec0 payload, assembled per-point by
# `ingest/worker.py::_build_points`.
#
# `acl` is `text[]`, not scalar: `SearchFilter.acl_any` means "any of these
# tags overlaps the chunk's ACL list" (see `vector/base.py`), and
# `QdrantVectorStore` confirms a chunk's ACL is itself a list. A scalar column
# cannot express list-overlap; `text[]` + a GIN index + the `&&` operator can,
# and pushed down natively - no post-filtering in Python the way
# `SqliteVecStore` has to (vec0 cannot hold array columns).
_ADD_METADATA_COLUMNS_SQL: tuple[str, ...] = (
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS source_id text NOT NULL DEFAULT ''",
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS acl text[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true",
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS vector_payload jsonb",
)

# The six btree/GIN indexes ADR-0008 ticket 06 requires for every
# `SearchFilter` field. This is a correctness fix, not an optimisation: a
# pgvector HNSW scan under a narrow filter (e.g. one document, ~0.5% of the
# corpus) silently returns an empty result set without these, because the
# planner has no cheaper option than exhausting HNSW's bounded candidate list
# and post-filtering - see the ticket's spike numbers. `tenant_id` and
# `document_id` already have declarative indexes on `Chunk`
# (`db/models.py`), so only the four new columns plus the HNSW index itself
# are added here.
_ADD_FILTER_INDEXES_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_chunk_source_id ON chunk (source_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_acl_gin ON chunk USING gin (acl)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_kind ON chunk (kind)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_is_current ON chunk (is_current)",
)

_HNSW_INDEX_NAME = "ix_chunk_embedding_hnsw"
_CREATE_HNSW_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_HNSW_INDEX_NAME} "
    "ON chunk USING hnsw (embedding vector_cosine_ops)"
)

# `vector_cosine_ops` matches `QdrantVectorStore`'s `Distance.COSINE`
# (`vector/qdrant_store.py`) - keeping the scoring convention identical
# across backends matters for ticket 10/11's A/B comparison.
#
# Tuning applies to the HNSW build statement only, via `SET LOCAL` inside its
# own transaction - never a global/session-wide setting. Values mirror the
# ticket's own 100k-scale spike (`maintenance_work_mem = '1GB'`); the
# ParadeDB container's `shm_size` (ticket 05) is a precondition for parallel
# workers to have anywhere to put shared state.
_HNSW_BUILD_TUNING_SQL: tuple[str, ...] = (
    "SET LOCAL maintenance_work_mem = '1GB'",
    "SET LOCAL max_parallel_maintenance_workers = 4",
)

_DIMENSION_SQL = text(
    "SELECT format_type(a.atttypid, a.atttypmod) AS coltype "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "WHERE c.relname = 'chunk' "
    "AND pg_table_is_visible(c.oid) "
    "AND a.attname = 'embedding' "
    "AND a.attnum > 0 "
    "AND NOT a.attisdropped"
)

_DIMENSION_RE = re.compile(r"vector\((\d+)\)")


def ensure_pgvector_schema(engine: Engine | None = None, *, dim: int) -> None:
    """Add the dense-retrieval columns and indexes to `chunk` if missing.

    Idempotent: every `ALTER TABLE`/`CREATE INDEX` is `IF NOT EXISTS`-guarded,
    the same additive-only shape `_apply_additive_columns` and
    `ensure_fts5_table` use (ADR-0004 forbids a migration framework). Callers
    are expected to have already checked `get_pgvector_dimension` against
    *dim* - this function does not re-check, so calling it with a *dim* that
    disagrees with an already-existing `embedding` column silently keeps the
    existing column (`ADD COLUMN IF NOT EXISTS` does not touch the type of an
    existing column).
    """
    if engine is None:
        engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(_ADD_EMBEDDING_COLUMN_SQL.format(dim=dim)))
        for stmt in _ADD_METADATA_COLUMNS_SQL:
            conn.execute(text(stmt))
        for stmt in _ADD_FILTER_INDEXES_SQL:
            conn.execute(text(stmt))
    # Separate transaction: the `SET LOCAL` tuning above must apply to the
    # HNSW build alone, not leak onto the additive ALTERs (harmless either
    # way) or - worse - onto whatever the caller's connection pool hands out
    # next if this ran on a pooled connection outside its own transaction.
    with engine.begin() as conn:
        for stmt in _HNSW_BUILD_TUNING_SQL:
            conn.execute(text(stmt))
        conn.execute(text(_CREATE_HNSW_INDEX_SQL))


def drop_pgvector_embedding(engine: Engine | None = None) -> None:
    """Drop the `embedding` column and its HNSW index.

    Used by `recreate_collection`. Deliberately narrow: `chunk` is not this
    store's table to drop or truncate - it holds metadata (text, ordinal,
    heading_path, ...) every other subsystem depends on (FTS5's source data,
    plagiarism, the repo layer). Only `embedding` and its index are dropped;
    `source_id`/`acl`/`is_current`/`vector_payload` are left as-is. A
    dimension change means re-embedding everyone anyway, so losing the vectors
    is expected - losing the metadata would not be.
    """
    if engine is None:
        engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(f"DROP INDEX IF EXISTS {_HNSW_INDEX_NAME}"))
        conn.execute(text("ALTER TABLE chunk DROP COLUMN IF EXISTS embedding"))


def get_pgvector_dimension(engine: Engine | None = None) -> int | None:
    """Return the declared width of `chunk.embedding`, or `None` if absent.

    Parses `format_type()`'s rendering of the column (`'vector(512)'`) rather
    than trusting a cached value, so a dimension change made outside this
    process (another deployment, a manual `ALTER`) is still detected -
    mirrors `get_vec0_dimension`'s role for sqlite-vec, reading
    `sqlite_master` rather than trusting a cached value for the same reason.
    """
    if engine is None:
        engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(_DIMENSION_SQL).first()
    if row is None:
        return None
    match = _DIMENSION_RE.search(row[0] or "")
    return int(match.group(1)) if match else None

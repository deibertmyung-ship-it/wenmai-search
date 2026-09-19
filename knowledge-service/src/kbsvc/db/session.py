"""Engine and session management."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models import Base, Tenant, utcnow

logger = logging.getLogger(__name__)

# Columns added to tables that already exist in deployed databases.
# `create_all` only ever creates whole tables, so it silently skips these.
#
# Deliberately not a migration framework: additive, nullable-or-defaulted
# columns only, listed by hand. Anything that rewrites or drops data belongs in
# a real tool, not in a startup hook that every process runs.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("chunk", "analyzed", "TEXT NOT NULL DEFAULT ''"),
)

# FTS5 virtual table DDL.  Created in `init_db` alongside the metadata tables
# because it has no runtime-discovered dimension.  `unicode61` keeps contiguous
# CJK characters as a single term (五行 stays one token), matching Tantivy's
# whitespace analyser on pre-analysed text.
#
# `version_id` and `payload` were added for ADR-0008 ticket 04
# (`Fts5LexicalStore`), edited into this DDL in place rather than migrated:
# no deployment has data in `chunk_fts` yet (ticket 02 only just landed), and
# FTS5 virtual tables reject `ALTER TABLE ... ADD COLUMN` outright ("virtual
# tables may not be altered", confirmed against the installed SQLite) - an
# ALTER-based migration was never on the table here.  `version_id` mirrors
# Tantivy's `version_id` term field, needed for `delete_by_versions`.
# `payload` is UNINDEXED (stored, not searched) and carries the same
# denormalized JSON view `chunk_vec_payload` carries for the vector side (see
# `db/vec_ddl.py`) - `ingest/reembed.py::_payload` calls this "the
# denormalized view both indexes carry, so a hit renders without a join".
# FTS5 has no companion-table restriction the way vec0 does, so it lives
# right in the row instead of a second table.
_FTS5_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5("
    "chunk_id UNINDEXED, tenant_id, document_id, version_id, source_id, kind, acl, "
    "is_current, body, payload UNINDEXED, tokenize='unicode61'"
    ")"
)


def ensure_fts5_table(bind: Engine | Connection | None = None) -> None:
    """Create the `chunk_fts` FTS5 virtual table if it does not exist.

    `init_db()` already does this as part of full schema setup.  This
    standalone entry point lets `Fts5LexicalStore.ensure_ready()` be
    self-sufficient the same way `ensure_vec0_table` lets `SqliteVecStore` be
    - a store built directly against a fresh database, without an `init_db()`
    call first (as the sqlite-vec and fts5 unit tests do, each pointing at a
    throwaway per-test database), still ends up with a usable table.
    Idempotent via `IF NOT EXISTS`.

    *bind* may be a live ``Connection`` (ticket 08's transactional publish,
    which needs the table ensured inside the worker's existing transaction -
    otherwise a second connection doing the DDL deadlocks on the first's
    SQLite write lock) or an ``Engine``.
    """
    if bind is None:
        bind = get_engine()
    if isinstance(bind, Connection):
        bind.execute(text(_FTS5_DDL))
        return
    with bind.begin() as conn:
        conn.execute(text(_FTS5_DDL))


def _load_sqlite_vec_extension(dbapi_conn) -> None:
    """Load the sqlite-vec extension onto *dbapi_conn*.

    Called from the ``connect`` event listener on every new SQLite connection.
    If ``sqlite_vec`` is not installed the error is actionable: it names the
    package and the install command, rather than letting the first query fail
    with an opaque ``no such module: vec0``.
    """
    try:
        import sqlite_vec
    except ImportError as exc:
        raise RuntimeError(
            "sqlite-vec extension is required for the local profile "
            "but the Python package 'sqlite-vec' is not installed. "
            "Install it with:  pip install sqlite-vec  "
            f"(original error: {exc})"
        ) from exc
    dbapi_conn.enable_load_extension(True)
    sqlite_vec.load(dbapi_conn)
    dbapi_conn.enable_load_extension(False)


def _make_engine(url: str) -> Engine:
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
            _load_sqlite_vec_extension(dbapi_conn)

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return _make_engine(get_settings().resolved_database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


# PostgreSQL extensions the server profile needs: pgvector's `vector` type
# (ticket 06) and pg_search's `bm25` index access method (ticket 07). ParadeDB
# images ship both compiled in and already listed in
# `shared_preload_libraries`, so `CREATE EXTENSION` only has to flip the
# catalog entry on - no build step, near-instant either way.
_POSTGRES_EXTENSIONS: tuple[str, ...] = ("pg_search", "vector")


def ensure_postgres_extensions(engine: Engine | None = None) -> None:
    """Create the pg_search and pgvector extensions if missing (ADR-0008 ticket 05).

    No-op against SQLite: gated on `engine.dialect.name`, the same
    dialect-driven distinction `_make_engine` uses for its `is_sqlite` branch,
    rather than a second way of asking the same question - local profile must
    stay completely unaffected. Idempotent via `IF NOT EXISTS`, the same
    additive-only shape `_apply_additive_columns` and `ensure_fts5_table` use:
    ADR-0004 forbids a migration framework.
    """
    if engine is None:
        engine = get_engine()
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for extension in _POSTGRES_EXTENSIONS:
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))


def _apply_additive_columns(engine: Engine) -> None:
    """Add `_ADDITIVE_COLUMNS` to tables that predate them.

    A fresh database gets them from `create_all` and this is a no-op; an
    existing one gets an `ALTER TABLE`. Both PostgreSQL 11+ and SQLite fill the
    new column from the DDL default without rewriting the table.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if table not in tables:
            continue
        if column in {col["name"] for col in inspector.get_columns(table)}:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        logger.info("added column %s.%s", table, column)


def init_db() -> None:
    """Create schema and ensure the default tenant exists."""
    engine = get_engine()
    # Extensions before tables: tickets 06/07 add `vector`/bm25-typed columns
    # through `_ADDITIVE_COLUMNS`-style ALTERs, which need the types to exist
    # first. No-op on SQLite.
    ensure_postgres_extensions(engine)
    Base.metadata.create_all(engine)
    _apply_additive_columns(engine)
    # FTS5 virtual table: no runtime-discovered dimension, so it belongs here
    # alongside the metadata tables.  Idempotent via IF NOT EXISTS.
    #
    # SQLite-only: `CREATE VIRTUAL TABLE ... USING fts5` is a syntax error on
    # PostgreSQL. `init_db()` only ever ran against SQLite before ADR-0008
    # ticket 05 made PostgreSQL a real, exercised target here (see
    # `ensure_postgres_extensions` above) - guarded the same way, by dialect,
    # since dialect is what actually determines which DDL is valid.
    if engine.dialect.name == "sqlite":
        ensure_fts5_table(engine)
    tenant_id = get_settings().default_tenant
    with session_scope() as session:
        if session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, name=tenant_id, created_at=utcnow()))


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """Used by tests that point KB_DATABASE_URL somewhere else."""
    get_engine.cache_clear()
    get_session_factory.cache_clear()

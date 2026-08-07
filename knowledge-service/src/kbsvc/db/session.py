"""Engine and session management."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, inspect, text
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

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return _make_engine(get_settings().resolved_database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


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
    Base.metadata.create_all(engine)
    _apply_additive_columns(engine)
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

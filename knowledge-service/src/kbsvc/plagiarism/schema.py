"""Schema lifecycle for the `plag_*` tables.

Kept out of `db/session.py:init_db()` on purpose: that runs on every profile
including SQLite, and these tables are PostgreSQL-only. Creating them is an
explicit operator step (`kbsvc plagiarism init`), not a side effect of starting
a process.

Follows ADR-0004: additive `create_all`, no migration framework. A formal tool
is listed as a follow-on decision in both ADR-0001 and ADR-0004.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session

from ..errors import KbError
from .models import PlagiarismBase

logger = logging.getLogger(__name__)

# Every table the feature needs. `verify_plagiarism_schema` reports on exactly
# this set, so a table added to models.py but forgotten here shows up as a
# passing readiness check on an incomplete schema.
REQUIRED_TABLES: tuple[str, ...] = (
    "plag_corpus_projection",
    "plag_corpus_chunk",
    "plag_fingerprint_df",
    "plag_corpus_job",
    "plag_check",
    "plag_check_source",
    "plag_check_passage",
    "plag_check_event",
    "plag_worker_heartbeat",
)

_GIN_INDEX = "ix_plag_chunk_fingerprints_gin"

_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("plag_check", "matcher_version", "TEXT NOT NULL DEFAULT ''"),
    ("plag_check", "matcher_config", "JSON NOT NULL DEFAULT '{}'::json"),
)


class PlagiarismUnavailableError(KbError):
    """The feature cannot run here - wrong dialect, or schema not created."""

    code = "feature_unavailable"
    http_status = 503


def is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


def ensure_postgres(engine: Engine) -> None:
    """Raise unless this engine speaks PostgreSQL.

    The error deliberately does not suggest a SQLite workaround: candidate
    retrieval is `BIGINT[]` overlap over a GIN index, and ADR-0001 rules out a
    scan-based fallback precisely so this never degrades silently.
    """
    if not is_postgres(engine):
        raise PlagiarismUnavailableError(
            "plagiarism detection requires PostgreSQL",
            {"dialect": engine.dialect.name},
        )


def init_plagiarism_schema(engine: Engine) -> list[str]:
    """Create any missing `plag_*` tables and indexes. Returns what was added.

    Idempotent - safe to re-run, and re-running is the normal way to pick up a
    newly added table.
    """
    ensure_postgres(engine)
    before = set(inspect(engine).get_table_names())
    PlagiarismBase.metadata.create_all(engine)
    _apply_additive_columns(engine)
    created = sorted(set(inspect(engine).get_table_names()) - before)
    if created:
        logger.info("created plagiarism tables: %s", ", ".join(created))
    return created


def _apply_additive_columns(engine: Engine) -> None:
    """Add new defaulted columns without rebuilding indexes or projections."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column, ddl_type in _ADDITIVE_COLUMNS:
        if table not in tables:
            continue
        columns = {item["name"] for item in inspector.get_columns(table)}
        if column in columns:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        logger.info("added column %s.%s", table, column)


def drop_plagiarism_schema(engine: Engine) -> None:
    """Drop every `plag_*` table. For tests and for a full rebuild from zero."""
    ensure_postgres(engine)
    PlagiarismBase.metadata.drop_all(engine)


def verify_plagiarism_schema(session: Session) -> dict:
    """Report schema readiness. Consumed by `/readyz` and `plagiarism status`.

    Checks the GIN index separately from the tables: the candidate query is
    correct without it but unusably slow, which is the failure mode that hides
    the longest.
    """
    engine = session.get_bind()
    if not is_postgres(engine):
        return {
            "ready": False,
            "dialect": engine.dialect.name,
            "reason": "requires PostgreSQL",
            "missing_tables": list(REQUIRED_TABLES),
            "missing_columns": [f"plag_check.{column}" for _, column, _ in _ADDITIVE_COLUMNS],
            "gin_index": False,
        }

    present = set(inspect(engine).get_table_names())
    missing = [name for name in REQUIRED_TABLES if name not in present]
    missing_columns: list[str] = []
    if "plag_check" in present:
        columns = {item["name"] for item in inspect(engine).get_columns("plag_check")}
        missing_columns = [
            f"{table}.{column}"
            for table, column, _ in _ADDITIVE_COLUMNS
            if column not in columns
        ]

    gin_present = False
    if "plag_corpus_chunk" in present:
        gin_present = bool(
            session.execute(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
                {"name": _GIN_INDEX},
            ).scalar()
        )

    return {
        "ready": not missing and not missing_columns and gin_present,
        "dialect": "postgresql",
        "missing_tables": missing,
        "missing_columns": missing_columns,
        "gin_index": gin_present,
    }

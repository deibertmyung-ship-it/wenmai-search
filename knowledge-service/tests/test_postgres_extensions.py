"""Extension-presence check for the ParadeDB deployment (ADR-0008 ticket 05).

`ensure_postgres_extensions` (`db/session.py`) creates `pg_search` and
`vector` idempotently whenever `init_db()` runs against PostgreSQL. This test
is the durable form of the ticket's "扩展可用，版本记录在案" acceptance
criterion: it proves both extensions are actually installed and at least at
the versions the ticket names, rather than leaving that as a one-time manual
check that leaves no trace.

Mirrors `tests/plagiarism/conftest.py`'s `pg_engine` fixture: point
`KB_TEST_POSTGRES_URL` at a reachable PostgreSQL/ParadeDB instance, or this
module skips loudly. With nothing reachable at that URL - the default local/CI
state, zero ParadeDB running - this module must skip cleanly, not error.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

# ADR-0008 ticket 05's floor. "At least" on purpose: a later ParadeDB rebuild
# that bumps either extension should not fail this check - only a regression
# below the pinned floor should.
_MIN_VERSIONS = {
    "pg_search": (0, 25, 2),
    "vector": (0, 8, 4),
}

# Default targets the compose stack from inside the compose network, same
# convention as tests/plagiarism/conftest.py. Override for a host-side run:
# postgresql+psycopg://kbsvc:...@localhost:5432/kbsvc_test
_DEFAULT_URL = "postgresql+psycopg://kbsvc:kbsvc@postgres:5432/kbsvc_test"

_SKIP_REASON = (
    "no PostgreSQL reachable at KB_TEST_POSTGRES_URL ({url}): {error}. "
    "ADR-0008 ticket 05's extension-presence check did NOT run."
)


def _version_tuple(extversion: str) -> tuple[int, ...]:
    """``'0.25.2'`` -> ``(0, 25, 2)``, for a plain numeric ``>=`` comparison."""
    return tuple(int(part) for part in extversion.split("."))


@pytest.fixture(scope="module")
def pg_engine():
    """Engine against the test database, or a loud skip.

    Module-scoped rather than importing the plagiarism suite's session-scoped
    fixture: conftest fixtures are only visible to tests under their own
    directory tree, and this check is about the deployment in general, not
    plagiarism specifically, so it lives at the top of `tests/` instead.
    """
    url = os.environ.get("KB_TEST_POSTGRES_URL", _DEFAULT_URL)
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any driver/network failure skips
        engine.dispose()
        pytest.skip(_SKIP_REASON.format(url=url, error=type(exc).__name__), allow_module_level=True)
    yield engine
    engine.dispose()


def test_extensions_meet_the_ticket_05_floor(pg_engine) -> None:
    """pg_search and vector must both be installed at >= the pinned floor.

    Also exercises idempotency: `ensure_postgres_extensions` must tolerate
    being called twice (init_db() calling it against an already-bootstrapped
    database is the normal case, not an edge case).
    """
    from kbsvc.db.session import ensure_postgres_extensions

    ensure_postgres_extensions(pg_engine)
    ensure_postgres_extensions(pg_engine)  # idempotent: must not raise

    with pg_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('pg_search', 'vector')"
            )
        ).all()
    versions = {row.extname: row.extversion for row in rows}

    assert set(versions) == set(_MIN_VERSIONS), f"missing extension(s), found {versions}"
    for extname, floor in _MIN_VERSIONS.items():
        actual = _version_tuple(versions[extname])
        assert actual >= floor, (
            f"{extname} {versions[extname]} is below the ticket 05 floor {floor}"
        )


def test_extension_bootstrap_is_a_noop_on_sqlite(tmp_path) -> None:
    """Local profile must be completely unaffected: no CREATE EXTENSION
    attempt against a SQLite engine. Needs no live PostgreSQL, so this always
    runs, even when the module's other test skips."""
    from kbsvc.db.session import ensure_postgres_extensions

    sqlite_engine = create_engine(f"sqlite:///{(tmp_path / 'noop.db').as_posix()}")
    try:
        ensure_postgres_extensions(sqlite_engine)  # must return silently, not raise
    finally:
        sqlite_engine.dispose()

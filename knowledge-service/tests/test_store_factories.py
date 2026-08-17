"""The two store factories dispatch on KB_VECTOR_BACKEND / KB_LEXICAL_BACKEND.

ADR-0008 keeps the old and new implementations side by side so the A/B gate can
load both. ``sqlite-vec`` (ticket 03), ``fts5`` (ticket 04), ``pgvector``
(ticket 06) and ``pg-search`` (ticket 07) are all implemented; the factory
constructs the right class for every named backend.
"""

from __future__ import annotations

import pytest

from kbsvc import lexical as lexical_pkg
from kbsvc import vector as vector_pkg
from kbsvc.config import reset_settings_cache
from kbsvc.errors import KbError
from kbsvc.lexical import (
    Fts5LexicalStore,
    PgSearchLexicalStore,
    TantivyLexicalStore,
    get_lexical_store,
    reset_lexical_store,
)
from kbsvc.vector import (
    PgVectorStore,
    QdrantVectorStore,
    SqliteVecStore,
    get_vector_store,
    reset_vector_store,
)

_PG_URL = "postgresql+psycopg://kbsvc:secret@localhost:5432/kbsvc"


@pytest.fixture
def select_backend(monkeypatch):
    """Move a switch for one test, rebuilding the singletons on both sides.

    Tantivy and embedded Qdrant take a directory lock, so a test that changes
    a switch cannot just drop the reference: it has to close the old store
    going in and leave a store built from the restored environment going out.
    The Postgres-backed stores share the engine and have no equivalent lock,
    but rebuilding both unconditionally keeps the fixture uniform across
    every backend pair.
    """

    def _rebuild() -> None:
        reset_vector_store()
        reset_lexical_store()
        reset_settings_cache()

    def _select(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        _rebuild()

    yield _select
    monkeypatch.undo()
    _rebuild()


def test_default_switches_build_the_pre_migration_stores() -> None:
    assert isinstance(get_vector_store(), QdrantVectorStore)
    assert isinstance(get_lexical_store(), TantivyLexicalStore)


def test_stores_stay_singletons_until_reset() -> None:
    assert get_vector_store() is get_vector_store()
    assert get_lexical_store() is get_lexical_store()


def test_sqlite_vec_backend_builds_sqlite_vec_store(select_backend) -> None:
    """sqlite-vec is implemented (ticket 03): the factory must return a
    SqliteVecStore, not raise."""
    select_backend(KB_VECTOR_BACKEND="sqlite-vec")
    store = get_vector_store()
    assert isinstance(store, SqliteVecStore)


def test_pgvector_backend_builds_pgvector_store(select_backend) -> None:
    """pgvector is implemented (ticket 06): the factory must return a
    PgVectorStore, not raise. Construction never opens a connection (the
    underlying `Engine` is lazy), so this needs no reachable PostgreSQL -
    unlike `ensure_collection`, which does."""
    select_backend(KB_VECTOR_BACKEND="pgvector", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)
    store = get_vector_store()
    assert isinstance(store, PgVectorStore)


def test_fts5_backend_builds_fts5_lexical_store(select_backend) -> None:
    """fts5 is implemented (ticket 04): the factory must return a
    Fts5LexicalStore, not raise."""
    select_backend(KB_LEXICAL_BACKEND="fts5")
    store = get_lexical_store()
    assert isinstance(store, Fts5LexicalStore)


def test_pg_search_backend_builds_pg_search_store(select_backend) -> None:
    """pg-search is implemented (ticket 07): the factory must return a
    PgSearchLexicalStore, not raise. Construction never opens a connection
    (the underlying `Engine` is lazy), so this needs no reachable
    PostgreSQL - unlike `ensure_ready`, which does (the lexical
    `get_lexical_store()` calls `ensure_ready()` after `_build_store()`,
    which is why this asserts on `_build_store()` directly rather than
    going through the singleton getter - the same "construction is lazy"
    property the pgvector test asserts, reached by a different path
    because the two factories differ on whether they auto-ensure)."""
    select_backend(
        KB_LEXICAL_BACKEND="pg-search", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL
    )
    store = lexical_pkg._build_store()
    assert isinstance(store, PgSearchLexicalStore)


def test_unknown_vector_backend_raises_at_the_factory(
    select_backend, monkeypatch
) -> None:
    """Defence in depth: the Settings model already rejects an unknown
    `KB_VECTOR_BACKEND` via its Literal type, but `_build_store`'s own
    `raise KbError("... not implemented")` branch must still fire if it is
    ever reached - e.g. by a caller constructing a Settings object with a
    value that bypasses env validation. Force it directly rather than
    relying on a real-but-magic string."""
    select_backend(KB_PROFILE="local")
    monkeypatch.setattr(vector_pkg, "get_settings", lambda: type(
        "S", (), {"vector_backend": "made-up-backend"}
    )())
    with pytest.raises(KbError, match="not implemented"):
        get_vector_store()


def test_unknown_lexical_backend_raises_at_the_factory(
    select_backend, monkeypatch
) -> None:
    """Lexical-side counterpart to the vector test above: with pg-search
    implemented in ticket 07, there is no longer a real-but-unimplemented
    backend name to use, so the branch is exercised by monkeypatching the
    settings getter directly."""
    select_backend(KB_PROFILE="local")
    monkeypatch.setattr(lexical_pkg, "get_settings", lambda: type(
        "S", (), {"lexical_backend": "made-up-backend"}
    )())
    with pytest.raises(KbError, match="not implemented"):
        get_lexical_store()


def test_reset_rebuilds_the_vector_store_against_the_current_switch(select_backend) -> None:
    """Proves a genuine backend swap rebuilds the singleton:
    qdrant -> pgvector -> qdrant."""
    original = get_vector_store()

    select_backend(KB_VECTOR_BACKEND="pgvector", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)
    swapped = get_vector_store()
    assert isinstance(swapped, PgVectorStore)
    assert swapped is not original

    select_backend(KB_VECTOR_BACKEND="qdrant", KB_PROFILE="local")
    rebuilt = get_vector_store()

    assert isinstance(rebuilt, QdrantVectorStore)
    assert rebuilt is not original
    assert rebuilt is not swapped


def test_reset_rebuilds_the_lexical_store_against_the_current_switch(select_backend) -> None:
    """Counterpart for the lexical store: tantivy -> fts5 -> tantivy.
    Swaps through fts5 rather than pg-search because the lexical
    `get_lexical_store()` calls `ensure_ready()` after construction, and
    pg-search's `ensure_ready()` opens a connection to run the bm25 DDL -
    that needs a reachable PostgreSQL, which this unit-level fixture does
    not provide. The pg-search *dispatch* is still verified by
    `test_pg_search_backend_builds_pg_search_store` above."""
    original = get_lexical_store()

    select_backend(KB_LEXICAL_BACKEND="fts5")
    swapped = get_lexical_store()
    assert isinstance(swapped, Fts5LexicalStore)
    assert swapped is not original

    select_backend(KB_LEXICAL_BACKEND="tantivy")
    rebuilt = get_lexical_store()

    assert isinstance(rebuilt, TantivyLexicalStore)
    assert rebuilt is not original
    assert rebuilt is not swapped


def test_a_failed_build_leaves_no_half_initialised_singleton(monkeypatch) -> None:
    """A failed `_build_store` must not cache a broken singleton: a second
    call retries the build, and `reset_*_store()` stays callable with
    nothing cached. With every named backend implemented as of ticket 07,
    there is no real-but-unimplemented name left to trigger this path
    naturally - monkeypatch the factory function itself to raise, the same
    guarantee either way."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated construction failure")

    monkeypatch.setattr(vector_pkg, "_build_store", _boom)
    with pytest.raises(RuntimeError, match="simulated"):
        get_vector_store()
    with pytest.raises(RuntimeError, match="simulated"):
        get_vector_store()
    reset_vector_store()

    monkeypatch.setattr(lexical_pkg, "_build_store", _boom)
    with pytest.raises(RuntimeError, match="simulated"):
        get_lexical_store()
    reset_lexical_store()

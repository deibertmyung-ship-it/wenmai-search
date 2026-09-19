"""The two store factories dispatch on KB_VECTOR_BACKEND / KB_LEXICAL_BACKEND."""

from __future__ import annotations

import pytest

from kbsvc import lexical as lexical_pkg
from kbsvc import vector as vector_pkg
from kbsvc.config import reset_settings_cache
from kbsvc.errors import KbError
from kbsvc.lexical import (
    Fts5LexicalStore,
    PgSearchLexicalStore,
    get_lexical_store,
    reset_lexical_store,
)
from kbsvc.vector import (
    PgVectorStore,
    SqliteVecStore,
    get_vector_store,
    reset_vector_store,
)

_PG_URL = "postgresql+psycopg://kbsvc:secret@localhost:5432/kbsvc"


@pytest.fixture
def select_backend(monkeypatch):
    """Move a switch for one test, rebuilding the singletons on both sides."""

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


def test_default_switches_build_the_post_migration_stores() -> None:
    assert isinstance(get_vector_store(), SqliteVecStore)
    assert isinstance(get_lexical_store(), Fts5LexicalStore)


def test_stores_stay_singletons_until_reset() -> None:
    assert get_vector_store() is get_vector_store()
    assert get_lexical_store() is get_lexical_store()


def test_sqlite_vec_backend_builds_sqlite_vec_store(select_backend) -> None:
    select_backend(KB_VECTOR_BACKEND="sqlite-vec")
    store = get_vector_store()
    assert isinstance(store, SqliteVecStore)


def test_pgvector_backend_builds_pgvector_store(select_backend) -> None:
    select_backend(KB_VECTOR_BACKEND="pgvector", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)
    store = get_vector_store()
    assert isinstance(store, PgVectorStore)


def test_fts5_backend_builds_fts5_lexical_store(select_backend) -> None:
    select_backend(KB_LEXICAL_BACKEND="fts5")
    store = get_lexical_store()
    assert isinstance(store, Fts5LexicalStore)


def test_pg_search_backend_builds_pg_search_store(select_backend) -> None:
    select_backend(
        KB_LEXICAL_BACKEND="pg-search", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL
    )
    store = lexical_pkg._build_store()
    assert isinstance(store, PgSearchLexicalStore)


def test_unknown_vector_backend_raises_at_the_factory(
    select_backend, monkeypatch
) -> None:
    select_backend(KB_PROFILE="local")
    monkeypatch.setattr(vector_pkg, "get_settings", lambda: type(
        "S", (), {"vector_backend": "made-up-backend"}
    )())
    with pytest.raises(KbError, match="not implemented"):
        get_vector_store()


def test_unknown_lexical_backend_raises_at_the_factory(
    select_backend, monkeypatch
) -> None:
    select_backend(KB_PROFILE="local")
    monkeypatch.setattr(lexical_pkg, "get_settings", lambda: type(
        "S", (), {"lexical_backend": "made-up-backend"}
    )())
    with pytest.raises(KbError, match="not implemented"):
        get_lexical_store()


def test_reset_rebuilds_the_vector_store_against_the_current_switch(select_backend) -> None:
    original = get_vector_store()

    select_backend(KB_VECTOR_BACKEND="pgvector", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)
    swapped = get_vector_store()
    assert isinstance(swapped, PgVectorStore)
    assert swapped is not original

    select_backend(KB_VECTOR_BACKEND="sqlite-vec", KB_PROFILE="local")
    rebuilt = get_vector_store()

    assert isinstance(rebuilt, SqliteVecStore)
    assert rebuilt is not original
    assert rebuilt is not swapped


def test_reset_rebuilds_the_lexical_store_against_the_current_switch(select_backend) -> None:
    original = get_lexical_store()
    reset_lexical_store()
    rebuilt = get_lexical_store()
    assert isinstance(rebuilt, Fts5LexicalStore)
    assert rebuilt is not original


def test_a_failed_build_leaves_no_half_initialised_singleton(monkeypatch) -> None:
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

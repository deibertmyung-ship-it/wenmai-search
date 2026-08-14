"""The two store factories dispatch on KB_VECTOR_BACKEND / KB_LEXICAL_BACKEND.

ADR-0008 keeps the old and new implementations side by side so the A/B gate can
load both. ``sqlite-vec`` (ticket 03) and ``fts5`` (ticket 04) are now
implemented; backends that are not yet implemented must fail loudly at the
factory rather than at the first query.
"""

from __future__ import annotations

import pytest

from kbsvc.config import reset_settings_cache
from kbsvc.errors import KbError
from kbsvc.lexical import (
    Fts5LexicalStore,
    TantivyLexicalStore,
    get_lexical_store,
    reset_lexical_store,
)
from kbsvc.vector import QdrantVectorStore, SqliteVecStore, get_vector_store, reset_vector_store

_PG_URL = "postgresql+psycopg://kbsvc:secret@localhost:5432/kbsvc"


@pytest.fixture
def select_backend(monkeypatch):
    """Move a switch for one test, rebuilding the singletons on both sides.

    Both stores hold a directory lock, so a test that changes a switch cannot
    just drop the reference: it has to close the old store going in and leave a
    store built from the restored environment going out.
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


def test_fts5_backend_builds_fts5_lexical_store(select_backend) -> None:
    """fts5 is implemented (ticket 04): the factory must return a
    Fts5LexicalStore, not raise."""
    select_backend(KB_LEXICAL_BACKEND="fts5")
    store = get_lexical_store()
    assert isinstance(store, Fts5LexicalStore)


@pytest.mark.parametrize(
    ("env", "getter"),
    [
        pytest.param(
            {"KB_VECTOR_BACKEND": "pgvector", "KB_PROFILE": "server", "KB_DATABASE_URL": _PG_URL},
            get_vector_store,
            id="pgvector",
        ),
        pytest.param(
            {"KB_LEXICAL_BACKEND": "pg-search", "KB_PROFILE": "server", "KB_DATABASE_URL": _PG_URL},
            get_lexical_store,
            id="pg-search",
        ),
    ],
)
def test_backends_without_an_implementation_fail_at_the_factory(
    select_backend, env, getter
) -> None:
    select_backend(**env)

    with pytest.raises(KbError, match="not implemented"):
        getter()


def test_reset_rebuilds_the_vector_store_against_the_current_switch(select_backend) -> None:
    original = get_vector_store()

    select_backend(KB_VECTOR_BACKEND="pgvector", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)
    with pytest.raises(KbError):
        get_vector_store()

    select_backend(KB_VECTOR_BACKEND="qdrant", KB_PROFILE="local")
    rebuilt = get_vector_store()

    assert isinstance(rebuilt, QdrantVectorStore)
    assert rebuilt is not original


def test_reset_rebuilds_the_lexical_store_against_the_current_switch(select_backend) -> None:
    original = get_lexical_store()

    # fts5 is implemented (ticket 04) and would no longer raise here, so this
    # still needs an unimplemented backend to exercise the failed-build path;
    # pg-search is the one remaining choice.
    select_backend(KB_LEXICAL_BACKEND="pg-search", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)
    with pytest.raises(KbError):
        get_lexical_store()

    select_backend(KB_LEXICAL_BACKEND="tantivy")
    rebuilt = get_lexical_store()

    assert isinstance(rebuilt, TantivyLexicalStore)
    assert rebuilt is not original


def test_a_failed_build_leaves_no_half_initialised_singleton(select_backend) -> None:
    select_backend(KB_VECTOR_BACKEND="pgvector", KB_PROFILE="server", KB_DATABASE_URL=_PG_URL)

    with pytest.raises(KbError):
        get_vector_store()

    # A second call must retry the build, not hand back a store that was never
    # constructed - and reset must stay callable with nothing cached.
    with pytest.raises(KbError):
        get_vector_store()
    reset_vector_store()

import pytest
from pydantic import ValidationError

from kbsvc.config import Settings, get_settings, reset_settings_cache

_PG_URL = "postgresql+psycopg://kbsvc:secret@localhost:5432/kbsvc"


@pytest.fixture
def settings_env(monkeypatch):
    """`get_settings()` is cached process-wide; rebuild it for one test only."""
    reset_settings_cache()
    yield monkeypatch
    monkeypatch.undo()
    reset_settings_cache()


def test_local_default_uses_embedded_qdrant() -> None:
    assert Settings.model_fields["qdrant_url"].default == ""


def test_local_embedded_mode_runs_worker_inside_api(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        profile="local",
        qdrant_url="",
        api_worker_enabled=None,
    )

    assert settings.use_embedded_qdrant
    assert settings.run_api_worker
    assert settings.qdrant_local_path == tmp_path / "qdrant"


def test_server_mode_keeps_api_worker_external(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        profile="server",
        qdrant_url="http://qdrant:6333",
        api_worker_enabled=None,
    )

    assert not settings.use_embedded_qdrant
    assert not settings.run_api_worker


def test_backend_switches_default_to_the_pre_migration_stores() -> None:
    assert Settings.model_fields["vector_backend"].default == "qdrant"
    assert Settings.model_fields["lexical_backend"].default == "tantivy"


def test_env_vars_select_the_backends(settings_env) -> None:
    settings_env.setenv("KB_VECTOR_BACKEND", "sqlite-vec")
    settings_env.setenv("KB_LEXICAL_BACKEND", "fts5")

    settings = get_settings()

    assert settings.vector_backend == "sqlite-vec"
    assert settings.lexical_backend == "fts5"


@pytest.mark.parametrize(
    ("profile", "vector_backend", "lexical_backend", "qdrant_url", "database_url", "expected"),
    [
        # Every combination reachable before the switches existed. These four
        # rows are the regression fence: their values may not move.
        pytest.param("local", "qdrant", "tantivy", "", "", True, id="local-embedded"),
        pytest.param(
            "local", "qdrant", "tantivy", "http://qdrant:6333", "", False, id="local-qdrant-server"
        ),
        pytest.param(
            "server", "qdrant", "tantivy", "http://qdrant:6333", _PG_URL, False, id="server"
        ),
        pytest.param("server", "qdrant", "tantivy", "", "", False, id="server-embedded"),
        # Stores this process opens itself keep the worker inside the API.
        pytest.param("local", "sqlite-vec", "fts5", "", "", True, id="local-single-file"),
        pytest.param("local", "sqlite-vec", "tantivy", "", "", True, id="local-sqlite-vec-only"),
        pytest.param("local", "qdrant", "fts5", "", "", True, id="local-fts5-only"),
        # Anything reached over the network takes concurrent writers, so the
        # worker stays an independently scalable process.
        pytest.param("local", "pgvector", "fts5", "", _PG_URL, False, id="local-pgvector"),
        pytest.param("local", "sqlite-vec", "pg-search", "", _PG_URL, False, id="local-pg-search"),
        pytest.param("server", "pgvector", "pg-search", "", _PG_URL, False, id="server-paradedb"),
    ],
)
def test_run_api_worker_follows_which_process_owns_the_stores(
    tmp_path, profile, vector_backend, lexical_backend, qdrant_url, database_url, expected
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        profile=profile,
        vector_backend=vector_backend,
        lexical_backend=lexical_backend,
        qdrant_url=qdrant_url,
        database_url=database_url,
        api_worker_enabled=None,
    )

    assert settings.run_api_worker is expected


@pytest.mark.parametrize(("profile", "override"), [("local", False), ("server", True)])
def test_api_worker_enabled_overrides_the_derivation(tmp_path, profile, override) -> None:
    settings = Settings(data_dir=tmp_path, profile=profile, api_worker_enabled=override)

    assert settings.run_api_worker is override


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"profile": "server", "vector_backend": "sqlite-vec", "database_url": _PG_URL},
            id="sqlite-vec-on-server",
        ),
        pytest.param(
            {"profile": "server", "lexical_backend": "fts5", "database_url": _PG_URL},
            id="fts5-on-server",
        ),
        pytest.param(
            {"profile": "server", "vector_backend": "pgvector"},
            id="pgvector-without-a-database-url",
        ),
        pytest.param(
            {"profile": "local", "vector_backend": "pgvector", "database_url": "sqlite:///kb.db"},
            id="pgvector-on-sqlite",
        ),
        pytest.param(
            {"profile": "server", "lexical_backend": "pg-search"},
            id="pg-search-without-a-database-url",
        ),
    ],
)
def test_backends_the_deployment_cannot_serve_are_rejected(tmp_path, overrides) -> None:
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, **overrides)


def test_an_illegal_combination_fails_at_get_settings(settings_env) -> None:
    settings_env.setenv("KB_PROFILE", "server")
    settings_env.setenv("KB_LEXICAL_BACKEND", "fts5")

    with pytest.raises(ValidationError):
        get_settings()


def test_postgres_backends_are_accepted_once_the_url_points_at_postgres(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        profile="server",
        vector_backend="pgvector",
        lexical_backend="pg-search",
        database_url=_PG_URL,
        api_worker_enabled=None,
    )

    assert settings.vector_backend == "pgvector"
    assert settings.lexical_backend == "pg-search"
    assert settings.run_api_worker is False


def test_projection_hash_ignores_matcher_thresholds_but_tracks_normalizer_version(tmp_path) -> None:
    base = Settings(data_dir=tmp_path, profile="local")
    changed_thresholds = Settings(
        data_dir=tmp_path,
        profile="local",
        plag_min_seed_len=31,
        plag_min_passage_len=51,
        plag_zh_min_seed_len=13,
        plag_zh_min_passage_len=21,
        plag_short_exact_min_score=0.98,
    )
    changed_normalizer = Settings(
        data_dir=tmp_path,
        profile="local",
        plag_normalizer_version="plag-normalizer-v3",
    )
    assert (
        base.plagiarism_algorithm_config_hash
        == changed_thresholds.plagiarism_algorithm_config_hash
    )
    assert (
        base.plagiarism_algorithm_config_hash
        != changed_normalizer.plagiarism_algorithm_config_hash
    )

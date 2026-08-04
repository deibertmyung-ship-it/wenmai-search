from kbsvc.config import Settings


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

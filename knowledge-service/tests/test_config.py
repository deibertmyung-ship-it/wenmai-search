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

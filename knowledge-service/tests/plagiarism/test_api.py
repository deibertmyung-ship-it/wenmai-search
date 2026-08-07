"""The HTTP seam - the spec's primary seam for plagiarism.

Almost every user-visible guarantee is asserted here rather than against the
service or the runner: two creation modes, ownership, ACL, error codes, feature
flags, idempotency, SSE replay and resumption.
"""

from __future__ import annotations

import json

import pytest
from _corpus import TENANT

pytest.importorskip("pysbd", reason="detection needs the `plagiarism` extra")

REUSED = (
    "贼克者，取用之首法也。上克下为贼，下贼上为克。"
    "凡四课之中，有上克下者为贼，下贼上者为克。"
    "取用之道，先取贼克，次取比用。涉害者，比用不成则涉害。"
)


@pytest.fixture
def api(app_db_on_test_postgres, monkeypatch):
    """A TestClient whose app is bound to the PostgreSQL test database."""
    from fastapi.testclient import TestClient

    from kbsvc.api.app import create_app
    from kbsvc.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "plag_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plag_min_seed_len", 12, raising=False)
    monkeypatch.setattr(settings, "plag_min_passage_len", 20, raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def api_corpus(api, kb_session, build_corpus):
    """A corpus the API can see. Committed - the API opens its own sessions."""
    document_id = build_corpus(text=REUSED)
    kb_session.commit()
    return document_id


def create_text(api, text=REUSED, **headers):
    return api.post("/v1/plagiarism/checks", json={"text": text}, headers=headers)


# --- feature gating -----------------------------------------------------


def test_routes_exist_and_answer(api):
    """Registered unconditionally: a missing route would make 'not enabled'
    indistinguishable from 'wrong URL'."""
    assert api.get("/v1/plagiarism/corpus/status").status_code != 404


def test_disabled_feature_returns_503_not_404(api, monkeypatch):
    from kbsvc.config import get_settings

    monkeypatch.setattr(get_settings(), "plag_enabled", False, raising=False)
    response = create_text(api)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "feature_disabled"


# --- creation -----------------------------------------------------------


def test_creating_a_text_check_returns_202_with_a_location(api, api_corpus):
    response = create_text(api)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["algorithm_config_hash"]
    assert response.headers["Location"].endswith(body["check_id"])


def test_creating_a_document_check_returns_202(api, api_corpus):
    response = api.post(f"/v1/plagiarism/checks/documents/{api_corpus}")
    assert response.status_code == 202
    assert response.json()["status"] == "pending"


def test_checking_an_unknown_document_is_404(api, api_corpus):
    response = api.post("/v1/plagiarism/checks/documents/no-such-document")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plagiarism_check_not_found"


def test_an_empty_corpus_is_409_not_an_empty_result(api):
    response = create_text(api)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plagiarism_corpus_empty"


def test_oversized_input_is_413(api, api_corpus, monkeypatch):
    from kbsvc.config import get_settings

    monkeypatch.setattr(get_settings(), "plag_max_input_chars", 50, raising=False)
    response = create_text(api, text="x" * 100)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "plagiarism_input_too_large"


def test_empty_text_is_rejected_by_validation(api, api_corpus):
    assert api.post("/v1/plagiarism/checks", json={"text": ""}).status_code == 422


# --- idempotency --------------------------------------------------------


def test_replaying_an_idempotency_key_returns_the_same_check(api, api_corpus):
    first = create_text(api, **{"Idempotency-Key": "abc"})
    second = create_text(api, **{"Idempotency-Key": "abc"})
    assert first.json()["check_id"] == second.json()["check_id"]


def test_reusing_a_key_for_different_content_is_409(api, api_corpus):
    create_text(api, **{"Idempotency-Key": "abc"})
    response = create_text(api, text=REUSED + "不同", **{"Idempotency-Key": "abc"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


def test_concurrency_limit_is_429(api, api_corpus, monkeypatch):
    from kbsvc.config import get_settings

    monkeypatch.setattr(get_settings(), "plag_max_active_checks_per_key", 1, raising=False)
    create_text(api)
    response = create_text(api)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "plagiarism_concurrency_limit"


# --- reads --------------------------------------------------------------


def test_get_check_and_list_never_expose_the_submitted_text(api, api_corpus):
    check_id = create_text(api).json()["check_id"]

    detail = api.get(f"/v1/plagiarism/checks/{check_id}")
    assert detail.status_code == 200
    assert "text" not in detail.json()
    assert detail.json()["query_chars"] == len(REUSED)

    listing = api.get("/v1/plagiarism/checks")
    assert listing.status_code == 200
    assert any(item["check_id"] == check_id for item in listing.json())
    assert all("text" not in item for item in listing.json())


def test_an_unknown_check_is_404(api, api_corpus):
    response = api.get("/v1/plagiarism/checks/does-not-exist")
    assert response.status_code == 404


def test_corpus_status_reports_readiness(api, api_corpus):
    body = api.get("/v1/plagiarism/corpus/status").json()
    assert body["total_documents"] == 1
    assert body["ready_documents"] == 1
    assert body["is_ready"] is True


def test_report_carries_offsets_and_completeness(api, api_corpus, kb_session):
    from kbsvc.config import get_settings
    from kbsvc.plagiarism.runner import CheckRunner

    check_id = create_text(api, text="前言。" + REUSED + "后记。").json()["check_id"]
    CheckRunner(get_settings()).run(kb_session, check_id)
    kb_session.commit()

    body = api.get(f"/v1/plagiarism/checks/{check_id}/report").json()
    assert body["sources"]
    source = body["sources"][0]
    assert source["content_hash"]
    assert source["passages"]
    passage = source["passages"][0]
    assert passage["query_end"] > passage["query_start"]
    assert len(passage["preview"]) <= get_settings().plag_preview_chars
    assert body["is_complete"] is True
    assert body["coverage_reason"] is None


# --- deletion -----------------------------------------------------------


def test_deleting_a_pending_check_returns_204(api, api_corpus):
    check_id = create_text(api).json()["check_id"]
    assert api.delete(f"/v1/plagiarism/checks/{check_id}").status_code == 204


def test_deleting_a_running_check_returns_202(api, api_corpus, kb_session):
    from kbsvc.plagiarism.models import PlagCheck
    from kbsvc.plagiarism.types import CheckStatus

    check_id = create_text(api).json()["check_id"]
    kb_session.query(PlagCheck).filter_by(id=check_id).update(
        {"status": str(CheckStatus.RUNNING)}
    )
    kb_session.commit()

    response = api.delete(f"/v1/plagiarism/checks/{check_id}")
    assert response.status_code == 202
    assert response.json()["outcome"] == "cancel_requested"


# --- SSE ----------------------------------------------------------------


def parse_frames(payload: str) -> list[dict]:
    """Parse an SSE body into `{id, event, data}` records."""
    frames = []
    for block in payload.strip().split("\n\n"):
        if not block.strip():
            continue
        record: dict = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            if key == "data":
                record["data"] = json.loads(value)
            elif key == "id":
                record["id"] = int(value)
            else:
                record[key] = value
        frames.append(record)
    return frames


def test_progress_replays_existing_events_then_closes_on_terminal(api, api_corpus, kb_session):
    """A late subscriber still sees the whole history - events are persisted,
    not broadcast."""
    from kbsvc.plagiarism import repository as repo
    from kbsvc.plagiarism.types import CheckStage, CheckStatus

    check_id = create_text(api).json()["check_id"]
    repo.append_event(
        kb_session,
        check_id=check_id,
        tenant_id=TENANT,
        stage=CheckStage.COMPLETED,
        status=CheckStatus.COMPLETED,
        progress=1.0,
    )
    kb_session.commit()

    with api.stream("GET", f"/v1/plagiarism/checks/{check_id}/progress") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = parse_frames("".join(response.iter_text()))

    assert [f["event"] for f in frames] == ["queued", "completed"]
    assert frames[0]["id"] < frames[1]["id"]


def test_last_event_id_resumes_without_gap_or_repeat(api, api_corpus, kb_session):
    from kbsvc.plagiarism import repository as repo
    from kbsvc.plagiarism.types import CheckStage, CheckStatus

    check_id = create_text(api).json()["check_id"]
    for stage in (CheckStage.STARTED, CheckStage.CHUNKING):
        repo.append_event(
            kb_session,
            check_id=check_id,
            tenant_id=TENANT,
            stage=stage,
            status=CheckStatus.RUNNING,
        )
    repo.append_event(
        kb_session,
        check_id=check_id,
        tenant_id=TENANT,
        stage=CheckStage.COMPLETED,
        status=CheckStatus.COMPLETED,
        progress=1.0,
    )
    kb_session.commit()

    with api.stream("GET", f"/v1/plagiarism/checks/{check_id}/progress") as response:
        everything = parse_frames("".join(response.iter_text()))
    assert [f["event"] for f in everything] == ["queued", "started", "chunking", "completed"]

    resume_from = everything[1]["id"]
    with api.stream(
        "GET",
        f"/v1/plagiarism/checks/{check_id}/progress",
        headers={"Last-Event-ID": str(resume_from)},
    ) as response:
        resumed = parse_frames("".join(response.iter_text()))

    assert [f["event"] for f in resumed] == ["chunking", "completed"]
    assert all(f["id"] > resume_from for f in resumed)


def test_progress_for_someone_elses_check_is_404(api, api_corpus, kb_session):
    from kbsvc.plagiarism.models import PlagCheck

    check_id = create_text(api).json()["check_id"]
    kb_session.query(PlagCheck).filter_by(id=check_id).update(
        {"creator_key_id": "somebody-else"}
    )
    kb_session.commit()
    assert api.get(f"/v1/plagiarism/checks/{check_id}/progress").status_code == 404


# --- readiness ----------------------------------------------------------


def test_readyz_reports_plagiarism_only_when_enabled(api, monkeypatch):
    from kbsvc.config import get_settings

    body = api.get("/readyz").json()
    assert "plagiarism_schema" in body

    monkeypatch.setattr(get_settings(), "plag_enabled", False, raising=False)
    assert "plagiarism_schema" not in api.get("/readyz").json()


def test_readyz_is_degraded_without_a_live_worker(api):
    """Checks are accepted whether or not a worker exists; readiness is where
    that shows up."""
    body = api.get("/readyz").json()
    assert body["plagiarism_worker"] == "no live worker"
    assert body["status"] == "degraded"

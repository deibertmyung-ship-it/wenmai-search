from __future__ import annotations

import time
from threading import Event

from fastapi.testclient import TestClient

from kbsvc.api.app import create_app
from kbsvc.ingest.worker import IngestWorker


def test_worker_loop_honors_stop_event(settings) -> None:
    stop_event = Event()
    stop_event.set()

    assert IngestWorker(settings=settings).run_forever(stop_event=stop_event) == 0


def test_api_owned_worker_processes_upload_and_shuts_down(settings) -> None:
    previous = settings.api_worker_enabled
    settings.api_worker_enabled = True
    app = create_app()

    try:
        with TestClient(app) as client:
            health = client.get("/healthz").json()
            assert health["worker"] == "running"

            source = client.post(
                "/v1/sources",
                json={"name": "api-worker-source", "kind": "upload"},
            ).json()
            registration = client.post(
                "/v1/ingest/upload",
                data={
                    "source_id": source["id"],
                    "external_id": "api/embedded-worker.md",
                    "title": "API 内置 worker 测试",
                },
                files={
                    "file": (
                        "embedded-worker.md",
                        "# 自动处理\n\n上传后由 API 内置常驻 worker 自动解析和索引。".encode(),
                        "text/markdown",
                    )
                },
            ).json()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = client.get(f"/v1/jobs/{registration['job_id']}").json()
                if job["state"] == "completed":
                    break
                time.sleep(0.05)

            assert job["state"] == "completed", job
            payload = client.post(
                "/v1/search",
                json={
                    "query": "上传后自动解析",
                    "filters": {"document_ids": [registration["document_id"]]},
                },
            ).json()
            assert payload["results"]

        assert not app.state.ingest_worker_thread.is_alive()
    finally:
        settings.api_worker_enabled = previous

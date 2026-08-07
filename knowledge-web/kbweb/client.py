"""Typed HTTP client for the kbsvc REST API.

Every backend call goes through here so that auth, timeouts and error
translation exist in exactly one place. The API key lives server-side only and
is never rendered into a page.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Config
from .errors import BackendError, BackendUnavailable

logger = logging.getLogger(__name__)


class KbClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        headers = {"Accept": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.Client(
            base_url=config.api_base, timeout=config.timeout, headers=headers
        )

    def close(self) -> None:
        self._client.close()

    # --- plumbing -------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("backend unreachable: %s %s (%s)", method, path, exc)
            raise BackendUnavailable(type(exc).__name__) from exc

        if response.status_code >= 400:
            raise BackendError(response.status_code, *_parse_error(response))
        if not response.content:
            return None
        return response.json()

    # --- retrieval ------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int,
        mode: str = "hybrid",
        source_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        title_contains: str | None = None,
        heading_contains: str | None = None,
        rerank: bool = True,
        rewrite: bool = True,
        debug: bool = False,
    ) -> dict:
        body = {
            "query": query,
            "top_k": top_k,
            "mode": mode,
            "rerank": rerank,
            "rewrite": rewrite,
            "debug": debug,
            "filters": {
                "source_ids": source_ids or None,
                "document_ids": document_ids or None,
                "title_contains": title_contains or None,
                "heading_contains": heading_contains or None,
                "current_only": True,
            },
        }
        return self._request("POST", "/v1/search", json=body)

    # --- library --------------------------------------------------------

    def list_sources(self) -> list[dict]:
        return self._request("GET", "/v1/sources") or []

    def create_source(self, name: str, kind: str = "upload", uri: str = "") -> dict:
        return self._request(
            "POST", "/v1/sources", json={"name": name, "kind": kind, "uri": uri}
        )

    def list_documents(
        self,
        *,
        source_id: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        params = {"limit": limit, "offset": offset}
        if source_id:
            params["source_id"] = source_id
        if q:
            params["q"] = q
        return self._request("GET", "/v1/documents", params=params) or []

    def get_document(self, document_id: str) -> dict:
        return self._request("GET", f"/v1/documents/{document_id}")

    def get_chunks(self, document_id: str, *, from_ordinal: int = 0, limit: int = 20) -> list[dict]:
        return (
            self._request(
                "GET",
                f"/v1/documents/{document_id}/chunks",
                params={"from_ordinal": from_ordinal, "limit": limit},
            )
            or []
        )

    def reindex_document(self, document_id: str) -> dict:
        return self._request("POST", f"/v1/documents/{document_id}/reindex")

    def delete_document(self, document_id: str) -> dict:
        return self._request("DELETE", f"/v1/documents/{document_id}")

    # --- ingest ---------------------------------------------------------

    def upload(
        self, *, source_id: str, filename: str, content: bytes, title: str = "", acl: str = ""
    ) -> dict:
        return self._request(
            "POST",
            "/v1/ingest/upload",
            data={
                "source_id": source_id,
                "external_id": filename,
                "title": title,
                "acl": acl,
            },
            files={"file": (filename, content)},
        )

    def ingest_path(
        self, *, source_id: str, path: str, patterns: list[str], recursive: bool = True
    ) -> dict:
        return self._request(
            "POST",
            "/v1/ingest/path",
            json={
                "source_id": source_id,
                "path": path,
                "patterns": patterns,
                "recursive": recursive,
            },
        )

    # --- jobs & ops -----------------------------------------------------

    def list_jobs(self, *, state: str | None = None, limit: int = 50) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if state:
            params["state"] = state
        return self._request("GET", "/v1/jobs", params=params) or []

    def retry_job(self, job_id: str) -> dict:
        return self._request("POST", f"/v1/jobs/{job_id}/retry")

    def stats(self) -> dict:
        return self._request("GET", "/v1/stats")

    def health(self) -> dict:
        return self._request("GET", "/healthz")


def _parse_error(response: httpx.Response) -> tuple[str, str, dict]:
    """kbsvc uses a single envelope; fall back gracefully if something else answers."""
    try:
        payload = response.json()
        error = payload.get("error") or {}
        if error:
            return (
                error.get("code", "backend_error"),
                error.get("message", "backend error"),
                error.get("detail", {}),
            )
        if "detail" in payload:  # FastAPI validation errors
            return "validation_error", "请求参数不合法", {"detail": payload["detail"]}
    except ValueError:
        pass
    return "backend_error", f"后端返回 {response.status_code}", {}

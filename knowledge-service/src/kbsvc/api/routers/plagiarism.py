"""Plagiarism detection endpoints.

Every route goes through `PlagiarismService`; none of them touch fingerprinting,
candidate retrieval or SQL. Errors are raised as `KbError` subclasses and turned
into the standard envelope by the app-level handler, so this module has no error
formatting of its own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...config import get_settings
from ...db.session import get_session_factory, session_scope
from ...plagiarism import PlagiarismService
from ...plagiarism.schema import PlagiarismUnavailableError, is_postgres
from ...plagiarism.service import FeatureDisabledError
from ...plagiarism.sse import stream_check_events
from ...plagiarism.types import CreateDocumentCheck, CreateTextCheck
from ..auth import Principal
from ..deps import get_principal, get_session
from ..plagiarism_schemas import (
    CheckCreated,
    CheckOut,
    CorpusStatusOut,
    ReportOut,
    TextCheckRequest,
)

router = APIRouter(prefix="/plagiarism", tags=["plagiarism"])


def _service(session: Session) -> PlagiarismService:
    """Guard dialect and feature flag before anything else.

    The feature is PostgreSQL-only (ADR-0001). A local SQLite install must still
    start and serve every other endpoint, so this fails per-request rather than
    at import or startup.

    The enabled check lives here, covering **every** route, rather than only on
    the create paths inside the service. `KB_PLAG_ENABLED=false` means the
    feature is not open, not "you may read but not write" - a disabled install
    was answering `GET /corpus/status` with real corpus counts, which
    contradicts both the runbook and the whole point of a release gate.
    """
    if not is_postgres(session.get_bind()):
        raise PlagiarismUnavailableError("plagiarism detection requires PostgreSQL")
    settings = get_settings()
    if not settings.plag_enabled:
        raise FeatureDisabledError("plagiarism detection is not enabled")
    return PlagiarismService(settings)


# --- creation -----------------------------------------------------------


@router.post("/checks", status_code=202)
def create_text_check(
    body: TextCheckRequest,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> CheckCreated:
    """Queue a check of submitted text. Returns 202 - nothing is computed here."""
    summary = _service(session).create_text_check(
        session,
        CreateTextCheck(
            tenant_id=principal.tenant_id,
            creator_key_id=principal.key_id or "",
            text=body.text,
            acl=principal.acl_filter or [],
            language=body.language,
            idempotency_key=idempotency_key,
        ),
    )
    response.headers["Location"] = f"/v1/plagiarism/checks/{summary.check_id}"
    return CheckCreated.of(summary)


@router.post("/checks/documents/{document_id}", status_code=202)
def create_document_check(
    document_id: str,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> CheckCreated:
    """Queue a check of an already-indexed document's current version."""
    summary = _service(session).create_document_check(
        session,
        CreateDocumentCheck(
            tenant_id=principal.tenant_id,
            creator_key_id=principal.key_id or "",
            document_id=document_id,
            acl=principal.acl_filter or [],
            idempotency_key=idempotency_key,
        ),
    )
    response.headers["Location"] = f"/v1/plagiarism/checks/{summary.check_id}"
    return CheckCreated.of(summary)


# --- reads --------------------------------------------------------------


@router.get("/checks")
def list_checks(
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[CheckOut]:
    summaries = _service(session).list_checks(
        session,
        tenant_id=principal.tenant_id,
        creator_key_id=principal.key_id or "",
        limit=limit,
        offset=offset,
    )
    return [CheckOut.of(summary) for summary in summaries]


@router.get("/checks/{check_id}")
def get_check(
    check_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> CheckOut:
    return CheckOut.of(
        _service(session).get_check(
            session,
            check_id=check_id,
            tenant_id=principal.tenant_id,
            creator_key_id=principal.key_id or "",
        )
    )


@router.get("/checks/{check_id}/report")
def get_report(
    check_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> ReportOut:
    """Findings with citable offsets.

    Source visibility is re-checked here, not just at creation - access can be
    revoked between running a check and reading its report.
    """
    return ReportOut.of(
        _service(session).get_report(
            session,
            check_id=check_id,
            tenant_id=principal.tenant_id,
            creator_key_id=principal.key_id or "",
        )
    )


@router.get("/corpus/status")
def corpus_status(
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> CorpusStatusOut:
    """Whether this caller's visible corpus can currently be checked against."""
    return CorpusStatusOut.of(
        _service(session).get_corpus_status(
            session, tenant_id=principal.tenant_id, acl=principal.acl_filter or []
        )
    )


# --- progress -----------------------------------------------------------


@router.get("/checks/{check_id}/progress")
def stream_progress(
    check_id: str,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
    last_event_id: str = Header(default="", alias="Last-Event-ID"),
) -> StreamingResponse:
    """Server-sent events for one check.

    Ownership is checked once here so an unauthorized caller gets a normal 404
    envelope rather than an empty stream, and again inside the generator on each
    poll - the stream outlives this request's transaction.
    """
    service = _service(session)
    service.get_check(
        session,
        check_id=check_id,
        tenant_id=principal.tenant_id,
        creator_key_id=principal.key_id or "",
    )

    cursor: int | None = None
    if last_event_id.strip().isdigit():
        cursor = int(last_event_id.strip())

    stream = stream_check_events(
        session_scope,
        check_id=check_id,
        tenant_id=principal.tenant_id,
        creator_key_id=principal.key_id or "",
        settings=get_settings(),
        last_event_id=cursor,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Proxies that buffer would defeat the point of streaming.
            "X-Accel-Buffering": "no",
        },
    )


# --- deletion -----------------------------------------------------------


@router.delete("/checks/{check_id}")
def delete_check(
    check_id: str,
    response: Response,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Delete, or request cancellation of a running check.

    204 when it is gone. 202 when a worker still owns it - cancellation is
    cooperative, so the caller is told the request was accepted, not completed.
    """
    outcome = _service(session).delete_check(
        session,
        check_id=check_id,
        tenant_id=principal.tenant_id,
        creator_key_id=principal.key_id or "",
    )
    if outcome == "cancel_requested":
        response.status_code = 202
    else:
        response.status_code = 204
    return {"check_id": check_id, "outcome": outcome}


__all__ = ["router", "get_session_factory"]

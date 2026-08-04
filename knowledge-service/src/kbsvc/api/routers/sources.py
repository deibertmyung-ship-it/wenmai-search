"""Source management."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import repo
from ...db.models import Document
from ..auth import Principal
from ..deps import get_principal, get_session
from ..schemas import SourceCreate, SourceOut

router = APIRouter(prefix="/sources", tags=["sources"])


def _document_counts(session: Session, tenant_id: str) -> dict[str, int]:
    stmt = (
        select(Document.source_id, func.count(Document.id))
        .where(Document.tenant_id == tenant_id, Document.deleted_at.is_(None))
        .group_by(Document.source_id)
    )
    return dict(session.execute(stmt).all())


@router.get("")
def list_sources(
    principal: Principal = Depends(get_principal), session: Session = Depends(get_session)
) -> list[SourceOut]:
    counts = _document_counts(session, principal.tenant_id)
    return [
        SourceOut(
            id=source.id,
            name=source.name,
            kind=source.kind,
            uri=source.uri,
            config=source.config,
            document_count=counts.get(source.id, 0),
        )
        for source in repo.list_sources(session, principal.tenant_id)
    ]


@router.post("", status_code=201)
def create_source(
    body: SourceCreate,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> SourceOut:
    source = repo.upsert_source(
        session,
        tenant_id=principal.tenant_id,
        name=body.name,
        kind=body.kind,
        uri=body.uri,
        config=body.config,
    )
    return SourceOut(
        id=source.id,
        name=source.name,
        kind=source.kind,
        uri=source.uri,
        config=source.config,
        document_count=0,
    )

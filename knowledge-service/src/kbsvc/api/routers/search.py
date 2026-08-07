"""Retrieval endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...db import repo
from ...retrieval.pipeline import RetrievalRequest, RetrievalResponse, get_retrieval_service
from ..auth import Principal
from ..deps import get_principal, get_session
from ..schemas import SearchRequest

router = APIRouter(tags=["search"])


@router.post("/search")
def search(
    body: SearchRequest,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> dict:
    document_ids = _resolve_documents(session, body, principal)
    if document_ids == []:
        # A title that matches nothing must return nothing. Falling through with
        # no document filter would search the whole corpus - the caller asked for
        # one book and would silently get hits from every other one.
        return RetrievalResponse(query=body.query, results=[], debug=None).to_dict()

    request = RetrievalRequest(
        query=body.query,
        tenant_id=principal.tenant_id,
        top_k=body.top_k,
        mode=body.mode,
        acl=principal.acl_filter,
        source_ids=body.filters.source_ids,
        document_ids=document_ids,
        kinds=body.filters.kinds,
        heading_contains=body.filters.heading_contains,
        current_only=body.filters.current_only,
        rerank=body.rerank,
        rewrite=body.rewrite,
        debug=body.debug,
    )
    return get_retrieval_service().search(request).to_dict()


def _resolve_documents(
    session: Session, body: SearchRequest, principal: Principal
) -> list[str] | None:
    """Fold `title_contains` into the document filter.

    Returns None for "no document filter", a non-empty list to search within, or
    an empty list meaning the title matched nothing - which the caller must treat
    as an empty result rather than as an absent filter.
    """
    if not body.filters.title_contains:
        return body.filters.document_ids

    matched = repo.document_ids_by_title(
        session,
        tenant_id=principal.tenant_id,
        needle=body.filters.title_contains,
        source_ids=body.filters.source_ids,
    )
    if not body.filters.document_ids:
        return matched
    # Both given: the caller wants the intersection, not the union.
    allowed = set(matched)
    return [document_id for document_id in body.filters.document_ids if document_id in allowed]

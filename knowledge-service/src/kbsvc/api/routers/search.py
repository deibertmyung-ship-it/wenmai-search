"""Retrieval endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...retrieval.pipeline import RetrievalRequest, get_retrieval_service
from ..auth import Principal
from ..deps import get_principal
from ..schemas import SearchRequest

router = APIRouter(tags=["search"])


@router.post("/search")
def search(body: SearchRequest, principal: Principal = Depends(get_principal)) -> dict:
    request = RetrievalRequest(
        query=body.query,
        tenant_id=principal.tenant_id,
        top_k=body.top_k,
        mode=body.mode,
        acl=principal.acl_filter,
        source_ids=body.filters.source_ids,
        document_ids=body.filters.document_ids,
        kinds=body.filters.kinds,
        heading_contains=body.filters.heading_contains,
        current_only=body.filters.current_only,
        rerank=body.rerank,
        rewrite=body.rewrite,
        debug=body.debug,
    )
    return get_retrieval_service().search(request).to_dict()

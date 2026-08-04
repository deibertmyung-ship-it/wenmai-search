"""Ingestion endpoints. These only register and enqueue - never parse inline."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from ...db import repo
from ...errors import NotFoundError, ValidationError
from ...ingest.uploader import register_bytes, register_path
from ..auth import Principal
from ..deps import get_principal, get_session
from ..schemas import BatchRegistrationOut, IngestPathRequest, RegistrationOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"])


def _require_source(session: Session, tenant_id: str, source_id: str) -> None:
    sources = {source.id for source in repo.list_sources(session, tenant_id)}
    if source_id not in sources:
        raise NotFoundError("source not found", {"source_id": source_id})


@router.post("/upload", status_code=202)
async def upload(
    file: UploadFile = File(...),
    source_id: str = Form(...),
    external_id: str = Form(default=""),
    title: str = Form(default=""),
    acl: str = Form(default=""),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> RegistrationOut:
    _require_source(session, principal.tenant_id, source_id)
    data = await file.read()
    acl_list = [tag.strip() for tag in acl.split(",") if tag.strip()] or None
    result = register_bytes(
        session,
        tenant_id=principal.tenant_id,
        source_id=source_id,
        external_id=external_id or file.filename or "",
        data=data,
        filename=file.filename or external_id,
        title=title,
        acl=acl_list,
    )
    return RegistrationOut(**result.__dict__)


@router.post("/path", status_code=202)
def ingest_path(
    body: IngestPathRequest,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> BatchRegistrationOut:
    """Bulk-register files the server can already read. Avoids HTTP for big corpora."""
    _require_source(session, principal.tenant_id, body.source_id)
    root = Path(body.path).expanduser()
    if not root.exists():
        raise ValidationError("path does not exist", {"path": str(root)})

    files = _collect_files(root, body.patterns, body.recursive)[: body.limit]
    items: list[RegistrationOut] = []
    errors: list[dict] = []
    deduplicated = 0

    for path in files:
        try:
            external_id = (
                path.relative_to(root).as_posix() if root.is_dir() else path.name
            )
            result = register_path(
                session,
                tenant_id=principal.tenant_id,
                source_id=body.source_id,
                path=path,
                external_id=external_id,
                acl=body.acl,
            )
            deduplicated += int(result.deduplicated)
            items.append(RegistrationOut(**result.__dict__))
        except Exception as exc:
            logger.warning("failed to register %s: %s", path, exc)
            errors.append({"path": str(path), "error": str(exc)})

    return BatchRegistrationOut(
        registered=len(items) - deduplicated,
        deduplicated=deduplicated,
        failed=len(errors),
        items=items,
        errors=errors,
    )


def _collect_files(root: Path, patterns: list[str], recursive: bool) -> list[Path]:
    if root.is_file():
        return [root]
    found: list[Path] = []
    for pattern in patterns or ["*"]:
        globber = root.rglob if recursive else root.glob
        found.extend(path for path in globber(pattern) if path.is_file())
    return sorted(set(found))

"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from ..db.session import get_session_factory
from .auth import Principal, extract_bearer, resolve_principal


def get_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_principal(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    return resolve_principal(session, extract_bearer(authorization) or x_api_key)

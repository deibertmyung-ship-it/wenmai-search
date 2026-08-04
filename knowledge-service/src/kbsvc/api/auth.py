"""API key authentication.

A principal is (tenant_id, acl). The client never supplies either - both are
derived server-side from the key, which is what keeps the MCP surface safe to
hand to an agent.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ids
from ..config import get_settings
from ..db.models import ApiKey
from ..errors import AuthError


@dataclass
class Principal:
    tenant_id: str
    acl: list[str] = field(default_factory=list)
    key_id: str | None = None

    @property
    def acl_filter(self) -> list[str] | None:
        """None means 'no ACL restriction' (anonymous/admin mode)."""
        return self.acl or None


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def issue_key(
    session: Session, *, tenant_id: str, name: str, acl: list[str] | None = None
) -> tuple[str, ApiKey]:
    """Create a key. The plaintext is returned once and never stored."""
    raw = f"kb_{secrets.token_urlsafe(32)}"
    row = ApiKey(
        id=ids.new_id(),
        tenant_id=tenant_id,
        key_hash=hash_key(raw),
        name=name,
        acl=acl or ["public"],
        is_active=True,
    )
    session.add(row)
    session.flush()
    return raw, row


def resolve_principal(session: Session, raw_key: str | None) -> Principal:
    settings = get_settings()
    if not raw_key:
        if settings.auth_required:
            raise AuthError("missing API key")
        return Principal(tenant_id=settings.default_tenant, acl=[])

    stmt = select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key), ApiKey.is_active.is_(True))
    row = session.scalars(stmt).first()
    if row is None:
        raise AuthError("invalid API key")
    return Principal(tenant_id=row.tenant_id, acl=list(row.acl or []), key_id=row.id)


def extract_bearer(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return header_value.strip()

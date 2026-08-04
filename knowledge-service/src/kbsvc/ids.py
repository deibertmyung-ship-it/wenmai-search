"""Stable identifier and content-hash derivation.

Every id in the system is derived, never random, so that re-running an import
converges on the same rows and the same Qdrant point ids.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from pathlib import Path

NS_ROOT = uuid.UUID("6f4b3d2a-1c8e-5f7a-9b0d-3e2a1c8e5f7a")
NS_SOURCE = uuid.uuid5(NS_ROOT, "source")
NS_DOCUMENT = uuid.uuid5(NS_ROOT, "document")
NS_VERSION = uuid.uuid5(NS_ROOT, "version")
NS_CHUNK = uuid.uuid5(NS_ROOT, "chunk")

_HASH_CHUNK_BYTES = 1024 * 1024


def _join(*parts: object) -> str:
    return "|".join(str(part) for part in parts)


def source_id(tenant_id: str, name: str) -> str:
    return str(uuid.uuid5(NS_SOURCE, _join(tenant_id, name)))


def document_id(tenant_id: str, source_id_: str, external_id: str) -> str:
    return str(uuid.uuid5(NS_DOCUMENT, _join(tenant_id, source_id_, external_id)))


def version_id(document_id_: str, content_hash: str) -> str:
    return str(uuid.uuid5(NS_VERSION, _join(document_id_, content_hash)))


def chunk_id(document_id_: str, version: int, ordinal: int, content_hash: str) -> str:
    return str(uuid.uuid5(NS_CHUNK, _join(document_id_, version, ordinal, content_hash)))


def new_id() -> str:
    """For rows with no natural key (jobs, events, api keys)."""
    return str(uuid.uuid4())


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(_HASH_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def hash_stream(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for block in chunks:
        digest.update(block)
    return digest.hexdigest()


def object_key(tenant_id: str, document_id_: str, content_hash: str, suffix: str) -> str:
    """Object storage key derived from hashes only - no user-controlled path segments."""
    clean_suffix = "".join(c for c in suffix if c.isalnum() or c == ".")[:16]
    if clean_suffix and not clean_suffix.startswith("."):
        clean_suffix = f".{clean_suffix}"
    safe_tenant = "".join(c for c in tenant_id if c.isalnum() or c in "-_")[:64] or "default"
    return f"{safe_tenant}/{document_id_}/{content_hash}{clean_suffix}"

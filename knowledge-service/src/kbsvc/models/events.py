"""Index event vocabulary. Updates and deletes must always emit one of these."""

from __future__ import annotations

from enum import StrEnum


class IndexEventType(StrEnum):
    CHUNKS_UPSERTED = "chunks_upserted"
    CHUNKS_DELETED = "chunks_deleted"
    VERSION_SUPERSEDED = "version_superseded"
    DOCUMENT_DELETED = "document_deleted"
    INDEX_FAILED = "index_failed"

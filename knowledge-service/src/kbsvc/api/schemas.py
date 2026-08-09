"""Request/response contracts for the REST API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SearchFilters(BaseModel):
    source_ids: list[str] | None = None
    document_ids: list[str] | None = None
    # Resolved to document_ids before retrieval, so the filter is pushed down to
    # both stores. `heading_contains` cannot be: headings are per chunk, not per
    # document, so it stays a post-fusion filter.
    title_contains: str | None = None
    heading_contains: str | None = None
    kinds: list[str] | None = None
    current_only: bool = True


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=100)
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    rerank: bool = True
    rewrite: bool = True
    debug: bool = False


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["upload", "filesystem", "url"] = "upload"
    uri: str = ""
    config: dict = Field(default_factory=dict)


class SourceOut(BaseModel):
    id: str
    name: str
    kind: str
    uri: str
    config: dict
    document_count: int = 0


class IngestPathRequest(BaseModel):
    source_id: str
    path: str
    recursive: bool = True
    patterns: list[str] = Field(default_factory=lambda: ["*"])
    acl: list[str] | None = None
    limit: int = Field(default=10_000, ge=1, le=100_000)


class RegistrationOut(BaseModel):
    document_id: str
    version_id: str
    job_id: str | None
    state: str
    deduplicated: bool


class BatchRegistrationOut(BaseModel):
    registered: int
    deduplicated: int
    failed: int
    items: list[RegistrationOut]
    errors: list[dict] = Field(default_factory=list)


class JobOut(BaseModel):
    id: str
    document_id: str
    version_id: str | None
    job_type: str
    state: str
    attempts: int
    max_attempts: int
    last_error: str | None
    scheduled_at: str
    created_at: str
    finished_at: str | None


class DocumentOut(BaseModel):
    id: str
    source_id: str
    external_id: str
    title: str
    acl: list[str]
    current_version_id: str | None
    version_count: int
    chunk_count: int
    created_at: str
    updated_at: str


class ChunkOut(BaseModel):
    chunk_id: str
    document_id: str
    version_id: str
    ordinal: int
    kind: str
    text: str
    token_count: int
    char_start: int
    char_end: int
    page_from: int | None
    page_to: int | None
    heading_path: list[str]
    content_hash: str


class HighlightRangeOut(BaseModel):
    local_start: int
    local_end: int
    document_start: int
    document_end: int


class PassageChunkOut(ChunkOut):
    highlights: list[HighlightRangeOut]


class PassageWindowOut(BaseModel):
    document_id: str
    version_id: str
    version: int
    requested_start: int
    requested_end: int
    focus_ordinal: int
    from_ordinal: int
    next_from: int
    has_more: bool
    exact: bool
    chunks: list[PassageChunkOut]


class StatsOut(BaseModel):
    tenant_id: str
    sources: int
    documents: int
    versions: int
    chunks: int
    vector_points: int
    # Retrieval has two indexes; showing only one hides a drift between them.
    lexical_docs: int
    jobs_by_state: dict[str, int]

"""Request and response models for the plagiarism endpoints.

Separate from `schemas.py` so the knowledge-base contract stays readable, and so
a deployment without the feature carries no plagiarism types in its OpenAPI.

SSE is not modelled here - it is a text stream, not a JSON response, and
pretending otherwise would put a misleading schema in the docs.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..plagiarism.types import CheckReport, CheckStatus, CheckSummary, CorpusStatus


class TextCheckRequest(BaseModel):
    text: str = Field(min_length=1)
    # Only "auto" and ISO 639-1 codes are meaningful; anything else falls back
    # to English segmentation rather than failing the request.
    language: str = "auto"


class CheckCreated(BaseModel):
    """202 body. The check is queued, not finished."""

    check_id: str
    status: CheckStatus
    snapshot_at: datetime
    algorithm_config_hash: str

    @classmethod
    def of(cls, summary: CheckSummary) -> CheckCreated:
        return cls(
            check_id=summary.check_id,
            status=summary.status,
            snapshot_at=summary.snapshot_at,
            algorithm_config_hash=summary.algorithm_config_hash,
        )


class CheckOut(BaseModel):
    """List/detail view. Never carries the submitted text."""

    check_id: str
    status: CheckStatus
    created_at: datetime
    snapshot_at: datetime
    algorithm_config_hash: str
    source_document_id: str | None = None
    query_chars: int = 0
    matched_chars: int = 0

    @classmethod
    def of(cls, summary: CheckSummary) -> CheckOut:
        return cls(
            check_id=summary.check_id,
            status=summary.status,
            created_at=summary.created_at,
            snapshot_at=summary.snapshot_at,
            algorithm_config_hash=summary.algorithm_config_hash,
            source_document_id=summary.source_document_id,
            query_chars=summary.query_chars,
            matched_chars=summary.matched_chars,
        )


class PassageOut(BaseModel):
    """Half-open `[start, end)` character offsets, in document coordinates."""

    query_start: int
    query_end: int
    source_start: int
    source_end: int
    score: float
    preview: str


class SourceOut(BaseModel):
    """Version and content hash are frozen at run time, so a finding stays
    checkable after the source document moves on."""

    document_id: str
    version_id: str
    version: int
    content_hash: str
    title: str
    matched_chars: int
    score: float
    passages: list[PassageOut] = Field(default_factory=list)


class ReportOut(BaseModel):
    check_id: str
    status: CheckStatus
    snapshot_at: datetime
    algorithm_config_hash: str
    matcher_version: str = ""
    matcher_config: dict = Field(default_factory=dict)
    query_chars: int
    matched_chars: int
    checked_chunks: int
    total_chunks: int
    # Absent means the whole input was examined. Present means it was not, and
    # the result must not be read as "no reuse".
    coverage_reason: str | None = None
    is_complete: bool
    sources: list[SourceOut] = Field(default_factory=list)
    unique_passages: list[tuple[int, int]] = Field(default_factory=list)
    # The detection-time snapshot of what was checked: the submitted text in
    # text mode, or the text resolved from the frozen source document version
    # in document mode. Always populated for checks run after this field was
    # added; document-mode checks that predate it return "" here rather than
    # being backfilled.
    query_text: str = ""

    @classmethod
    def of(cls, report: CheckReport) -> ReportOut:
        return cls(
            check_id=report.check_id,
            status=report.status,
            snapshot_at=report.snapshot_at,
            algorithm_config_hash=report.algorithm_config_hash,
            matcher_version=report.matcher_version,
            matcher_config=report.matcher_config,
            query_chars=report.query_chars,
            matched_chars=report.matched_chars,
            checked_chunks=report.checked_chunks,
            total_chunks=report.total_chunks,
            coverage_reason=str(report.coverage_reason) if report.coverage_reason else None,
            is_complete=report.is_complete,
            query_text=report.query_text,
            sources=[
                SourceOut(
                    document_id=source.document_id,
                    version_id=source.version_id,
                    version=source.version,
                    content_hash=source.content_hash,
                    title=source.title,
                    matched_chars=source.matched_chars,
                    score=source.score,
                    passages=[
                        PassageOut(
                            query_start=p.query_start,
                            query_end=p.query_end,
                            source_start=p.source_start,
                            source_end=p.source_end,
                            score=p.score,
                            preview=p.preview,
                        )
                        for p in source.passages
                    ],
                )
                for source in report.sources
            ],
            unique_passages=report.unique_passages,
        )


class CorpusStatusOut(BaseModel):
    total_documents: int
    ready_documents: int
    pending_documents: int
    failed_documents: int
    algorithm_config_hash: str
    is_ready: bool

    @classmethod
    def of(cls, status: CorpusStatus) -> CorpusStatusOut:
        return cls(
            total_documents=status.total_documents,
            ready_documents=status.ready_documents,
            pending_documents=status.pending_documents,
            failed_documents=status.failed_documents,
            algorithm_config_hash=status.algorithm_config_hash,
            is_ready=status.is_ready,
        )

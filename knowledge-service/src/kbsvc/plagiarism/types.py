"""Domain types for plagiarism detection.

Not ported - these describe kbsvc's contract, which differs from upstream's.
The status set and the report shape are fixed by the spec
(`docs/specs/2026-08-07-plagiarism-detection-backend.md`) and are what the HTTP
layer and the SSE stream are written against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class CheckStatus(StrEnum):
    """Lifecycle of a single detection job.

    `COMPLETED_PARTIAL` is deliberately distinct from `COMPLETED`: a run that
    exhausted its time budget has real findings but has *not* cleared the rest
    of the input, and must never be read as "no plagiarism found".
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


TERMINAL_CHECK_STATUSES: frozenset[CheckStatus] = frozenset(
    {
        CheckStatus.COMPLETED,
        CheckStatus.COMPLETED_PARTIAL,
        CheckStatus.FAILED,
        CheckStatus.CANCELLED,
    }
)

# Statuses that count against a caller's concurrency allowance.
ACTIVE_CHECK_STATUSES: frozenset[CheckStatus] = frozenset(
    {CheckStatus.PENDING, CheckStatus.RUNNING, CheckStatus.CANCEL_REQUESTED}
)


class CorpusJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_CORPUS_STATUSES: frozenset[CorpusJobStatus] = frozenset(
    {CorpusJobStatus.COMPLETED, CorpusJobStatus.FAILED}
)


class CoverageReason(StrEnum):
    """Why a report does not cover the whole input. Absent means full coverage."""

    TIME_CAP = "time_cap"
    CANCELLED = "cancelled"


class CheckStage(StrEnum):
    """SSE event names. `KEEPALIVE` is transport-only and never persisted."""

    QUEUED = "queued"
    STARTED = "started"
    CHUNKING = "chunking"
    RETRIEVING = "retrieving"
    ALIGNING = "aligning"
    PERSISTING = "persisting"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    KEEPALIVE = "keepalive"


TERMINAL_STAGES: frozenset[CheckStage] = frozenset(
    {
        CheckStage.COMPLETED,
        CheckStage.COMPLETED_PARTIAL,
        CheckStage.FAILED,
        CheckStage.CANCELLED,
    }
)


@dataclass(frozen=True)
class CreateTextCheck:
    """Command: check a caller-supplied string."""

    tenant_id: str
    creator_key_id: str
    text: str
    acl: list[str] = field(default_factory=list)
    language: str = "auto"
    idempotency_key: str = ""


@dataclass(frozen=True)
class CreateDocumentCheck:
    """Command: check the current version of an already-indexed document.

    The *logical* document id is recorded so that every version of it can be
    excluded from candidates - excluding only the current version would let an
    earlier revision of the same work match itself.
    """

    tenant_id: str
    creator_key_id: str
    document_id: str
    acl: list[str] = field(default_factory=list)
    idempotency_key: str = ""


@dataclass(frozen=True)
class MatchedPassage:
    """One aligned span. Offsets are half-open `[start, end)` character indices."""

    query_start: int
    query_end: int
    source_start: int
    source_end: int
    score: float
    preview: str


@dataclass(frozen=True)
class MatchedSource:
    """Per-source rollup. Version and content hash are frozen at run time so the
    finding stays checkable after the document moves on."""

    document_id: str
    version_id: str
    version: int
    content_hash: str
    title: str
    matched_chars: int
    score: float
    passages: list[MatchedPassage] = field(default_factory=list)


@dataclass(frozen=True)
class CheckReport:
    check_id: str
    status: CheckStatus
    snapshot_at: datetime
    algorithm_config_hash: str
    query_chars: int
    matched_chars: int
    checked_chunks: int
    total_chunks: int
    sources: list[MatchedSource] = field(default_factory=list)
    unique_passages: list[tuple[int, int]] = field(default_factory=list)
    coverage_reason: CoverageReason | None = None

    @property
    def is_complete(self) -> bool:
        return self.coverage_reason is None and self.checked_chunks == self.total_chunks


@dataclass(frozen=True)
class CheckSummary:
    """List/detail view. Deliberately excludes the submitted text."""

    check_id: str
    status: CheckStatus
    created_at: datetime
    snapshot_at: datetime
    algorithm_config_hash: str
    source_document_id: str | None = None
    matched_chars: int = 0
    query_chars: int = 0


@dataclass(frozen=True)
class CheckEvent:
    """A persisted progress event. `event_id` is the SSE `id:` field and is
    monotonic per check, which is what makes `Last-Event-ID` resumption exact."""

    event_id: int
    check_id: str
    stage: CheckStage
    status: CheckStatus
    progress: float
    detail: dict
    created_at: datetime


@dataclass(frozen=True)
class CorpusStatus:
    """Readiness of the corpus visible to one caller."""

    total_documents: int
    ready_documents: int
    pending_documents: int
    failed_documents: int
    algorithm_config_hash: str

    @property
    def is_ready(self) -> bool:
        return self.total_documents > 0 and self.ready_documents == self.total_documents

    @property
    def is_empty(self) -> bool:
        return self.total_documents == 0

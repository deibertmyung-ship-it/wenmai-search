"""Ingest job state machine.

Kept as data, not scattered `if` statements, so that illegal transitions are a
test assertion rather than a production surprise.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"


ACTIVE_STATES = frozenset(
    {JobState.PARSING, JobState.CHUNKING, JobState.EMBEDDING, JobState.INDEXING}
)
TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})

TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.PARSING, JobState.CANCELLED}),
    JobState.PARSING: frozenset({JobState.CHUNKING, JobState.RETRY_WAIT, JobState.FAILED}),
    JobState.CHUNKING: frozenset({JobState.EMBEDDING, JobState.RETRY_WAIT, JobState.FAILED}),
    JobState.EMBEDDING: frozenset({JobState.INDEXING, JobState.RETRY_WAIT, JobState.FAILED}),
    JobState.INDEXING: frozenset({JobState.COMPLETED, JobState.RETRY_WAIT, JobState.FAILED}),
    JobState.RETRY_WAIT: frozenset({JobState.PENDING, JobState.PARSING, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset({JobState.PENDING}),
    JobState.CANCELLED: frozenset({JobState.PENDING}),
}


def can_transition(current: str, target: str) -> bool:
    try:
        return JobState(target) in TRANSITIONS[JobState(current)]
    except (KeyError, ValueError):
        return False


def backoff_seconds(attempts: int, *, base: float, cap: float) -> float:
    """Exponential backoff, capped. attempts is 1-based (first failure -> base)."""
    return min(base * (2 ** max(attempts - 1, 0)), cap)

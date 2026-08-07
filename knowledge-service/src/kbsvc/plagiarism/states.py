"""Allowed state transitions for checks and corpus jobs.

Centralised so neither the HTTP layer nor the worker can write an arbitrary
status. Every transition goes through `advance_check` / `advance_corpus_job`,
which raise rather than silently accept a nonsensical move - a check that goes
`completed -> running` is a bug worth failing loudly on, not a state to tolerate.

Mirrors the approach already used by `ingest/states.py` for ingest jobs.
"""

from __future__ import annotations

from ..errors import ValidationError
from .types import (
    TERMINAL_CHECK_STATUSES,
    TERMINAL_CORPUS_STATUSES,
    CheckStatus,
    CorpusJobStatus,
)

_CHECK_TRANSITIONS: dict[CheckStatus, frozenset[CheckStatus]] = {
    CheckStatus.PENDING: frozenset(
        {
            CheckStatus.RUNNING,
            # Cancelling a job that has not started skips the cooperative
            # handshake - there is no worker mid-flight to negotiate with.
            CheckStatus.CANCELLED,
            CheckStatus.FAILED,
        }
    ),
    CheckStatus.RUNNING: frozenset(
        {
            CheckStatus.COMPLETED,
            CheckStatus.COMPLETED_PARTIAL,
            CheckStatus.FAILED,
            CheckStatus.CANCEL_REQUESTED,
            # Lease expiry hands the job back to the queue for another attempt.
            CheckStatus.PENDING,
        }
    ),
    CheckStatus.CANCEL_REQUESTED: frozenset(
        {
            CheckStatus.CANCELLED,
            # The worker may reach a terminal state before it notices the
            # cancel flag. Losing that race is fine; discarding a finished
            # result to honour a late cancel would not be.
            CheckStatus.COMPLETED,
            CheckStatus.COMPLETED_PARTIAL,
            CheckStatus.FAILED,
        }
    ),
    CheckStatus.COMPLETED: frozenset(),
    CheckStatus.COMPLETED_PARTIAL: frozenset(),
    CheckStatus.FAILED: frozenset(),
    CheckStatus.CANCELLED: frozenset(),
}

_CORPUS_TRANSITIONS: dict[CorpusJobStatus, frozenset[CorpusJobStatus]] = {
    CorpusJobStatus.PENDING: frozenset({CorpusJobStatus.RUNNING, CorpusJobStatus.FAILED}),
    CorpusJobStatus.RUNNING: frozenset(
        {
            CorpusJobStatus.COMPLETED,
            CorpusJobStatus.FAILED,
            # Lease expiry, same as checks.
            CorpusJobStatus.PENDING,
        }
    ),
    CorpusJobStatus.COMPLETED: frozenset(),
    CorpusJobStatus.FAILED: frozenset({CorpusJobStatus.PENDING}),  # retry
}


def can_transition_check(current: CheckStatus, target: CheckStatus) -> bool:
    return target in _CHECK_TRANSITIONS.get(current, frozenset())


def advance_check(current: CheckStatus, target: CheckStatus) -> CheckStatus:
    """Return `target`, or raise if the move is not allowed."""
    if not can_transition_check(current, target):
        raise ValidationError(
            "illegal check state transition",
            {"from": str(current), "to": str(target)},
        )
    return target


def is_check_terminal(status: CheckStatus) -> bool:
    return status in TERMINAL_CHECK_STATUSES


def can_transition_corpus_job(current: CorpusJobStatus, target: CorpusJobStatus) -> bool:
    return target in _CORPUS_TRANSITIONS.get(current, frozenset())


def advance_corpus_job(current: CorpusJobStatus, target: CorpusJobStatus) -> CorpusJobStatus:
    if not can_transition_corpus_job(current, target):
        raise ValidationError(
            "illegal corpus job state transition",
            {"from": str(current), "to": str(target)},
        )
    return target


def is_corpus_job_terminal(status: CorpusJobStatus) -> bool:
    return status in TERMINAL_CORPUS_STATUSES

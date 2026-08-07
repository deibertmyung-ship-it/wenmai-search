"""Check and corpus-job state machines.

Not a port - this contract is kbsvc's own. Pure functions, no database.
"""

from __future__ import annotations

import pytest

from kbsvc.errors import ValidationError
from kbsvc.plagiarism.states import (
    advance_check,
    advance_corpus_job,
    can_transition_check,
    is_check_terminal,
    is_corpus_job_terminal,
)
from kbsvc.plagiarism.types import (
    ACTIVE_CHECK_STATUSES,
    TERMINAL_CHECK_STATUSES,
    CheckStatus,
    CorpusJobStatus,
)


def test_happy_path_runs_to_completion():
    chain = [
        (CheckStatus.PENDING, CheckStatus.RUNNING),
        (CheckStatus.RUNNING, CheckStatus.COMPLETED),
    ]
    assert all(can_transition_check(a, b) for a, b in chain)


def test_time_budget_exhaustion_is_a_distinct_terminal_state():
    assert can_transition_check(CheckStatus.RUNNING, CheckStatus.COMPLETED_PARTIAL)
    assert is_check_terminal(CheckStatus.COMPLETED_PARTIAL)


def test_every_terminal_status_is_final():
    for status in TERMINAL_CHECK_STATUSES:
        assert is_check_terminal(status)
        for target in CheckStatus:
            assert not can_transition_check(status, target)


def test_pending_cancels_without_the_cooperative_handshake():
    """Nothing is mid-flight, so there is no worker to negotiate with."""
    assert can_transition_check(CheckStatus.PENDING, CheckStatus.CANCELLED)
    assert not can_transition_check(CheckStatus.PENDING, CheckStatus.CANCEL_REQUESTED)


def test_running_cancel_goes_through_cancel_requested():
    assert can_transition_check(CheckStatus.RUNNING, CheckStatus.CANCEL_REQUESTED)
    assert not can_transition_check(CheckStatus.RUNNING, CheckStatus.CANCELLED)


def test_worker_may_finish_before_noticing_a_late_cancel():
    """Losing that race is acceptable; throwing away a finished result to
    honour a cancel that arrived afterwards is not."""
    for target in (
        CheckStatus.COMPLETED,
        CheckStatus.COMPLETED_PARTIAL,
        CheckStatus.FAILED,
    ):
        assert can_transition_check(CheckStatus.CANCEL_REQUESTED, target)


def test_expired_lease_returns_a_running_check_to_the_queue():
    assert can_transition_check(CheckStatus.RUNNING, CheckStatus.PENDING)


def test_advance_returns_the_target_on_a_legal_move():
    assert advance_check(CheckStatus.PENDING, CheckStatus.RUNNING) is CheckStatus.RUNNING


def test_advance_raises_rather_than_silently_accepting():
    with pytest.raises(ValidationError):
        advance_check(CheckStatus.COMPLETED, CheckStatus.RUNNING)


def test_active_statuses_are_exactly_the_non_terminal_ones():
    """The concurrency limit counts active checks; if these two sets ever drift,
    a caller could hold slots that never free up."""
    assert set(CheckStatus) - TERMINAL_CHECK_STATUSES == ACTIVE_CHECK_STATUSES


# --- corpus jobs --------------------------------------------------------


def test_corpus_job_happy_path():
    assert advance_corpus_job(CorpusJobStatus.PENDING, CorpusJobStatus.RUNNING)
    assert advance_corpus_job(CorpusJobStatus.RUNNING, CorpusJobStatus.COMPLETED)


def test_failed_corpus_job_can_be_requeued():
    """Projection builds are retried; a failed one is not a dead end."""
    assert advance_corpus_job(CorpusJobStatus.FAILED, CorpusJobStatus.PENDING)


def test_completed_corpus_job_is_final():
    assert is_corpus_job_terminal(CorpusJobStatus.COMPLETED)
    with pytest.raises(ValidationError):
        advance_corpus_job(CorpusJobStatus.COMPLETED, CorpusJobStatus.RUNNING)

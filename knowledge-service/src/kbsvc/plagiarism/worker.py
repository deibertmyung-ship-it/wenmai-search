"""The plagiarism worker - a process of its own.

Separate from `IngestWorker` on purpose: alignment is CPU-bound, and sharing a
process would let a long check stall parsing, embedding and index writes.

Corpus jobs are drained before checks. A newly ingested document that has no
projection yet blocks *every* check (the corpus-readiness gate), so leaving
builds behind detection work would let one slow check keep the whole corpus
un-checkable.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import timedelta
from threading import Event

from ..config import Settings, get_settings
from ..db.session import session_scope
from . import repository as repo
from .projection import ProjectionBuilder
from .runner import CheckRunner
from .states import advance_check
from .types import CheckStage, CheckStatus, CorpusJobStatus

logger = logging.getLogger(__name__)

# Terminal status -> the event name that closes the stream for it.
_STAGE_FOR = {
    CheckStatus.COMPLETED: CheckStage.COMPLETED,
    CheckStatus.COMPLETED_PARTIAL: CheckStage.COMPLETED_PARTIAL,
    CheckStatus.FAILED: CheckStage.FAILED,
    CheckStatus.CANCELLED: CheckStage.CANCELLED,
}


class PlagiarismWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self._stop = Event()

    # --- lifecycle ------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        logger.info("plagiarism worker %s started", self.worker_id)
        while not self._stop.is_set():
            did_work = self.run_once()
            if not did_work:
                self._stop.wait(self.settings.plag_poll_interval)

    def drain(self, max_jobs: int = 1000) -> int:
        """Work until the queues are empty. For tests and one-shot runs."""
        done = 0
        while done < max_jobs and self.run_once():
            done += 1
        return done

    def run_once(self) -> bool:
        """Do one unit of work. Returns False when both queues are empty."""
        self._heartbeat()
        self._reclaim()
        if self._run_corpus_job():
            return True
        return self._run_check()

    # --- steps ----------------------------------------------------------

    def _heartbeat(self) -> None:
        with session_scope() as session:
            repo.record_heartbeat(
                session,
                worker_id=self.worker_id,
                algorithm_config_hash=self.settings.plagiarism_algorithm_config_hash,
            )

    def _reclaim(self) -> None:
        with session_scope() as session:
            corpus = repo.reclaim_expired_corpus_jobs(session)
            checks = repo.reclaim_expired_checks(session)
        if corpus or checks:
            logger.info("reclaimed %d corpus job(s), %d check(s)", corpus, checks)

    def _run_corpus_job(self) -> bool:
        with session_scope() as session:
            job = repo.claim_corpus_job(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.plag_lease_seconds,
            )
            if job is None:
                return False
            job_id = job.id

        try:
            with session_scope() as session:
                result = ProjectionBuilder(self.settings).build(session, job_id)
                job = session.get(repo.PlagCorpusJob, job_id)
                repo.finish_corpus_job(session, job, status=CorpusJobStatus.COMPLETED)
            logger.info("built projection for job %s: %s", job_id, result)
        except Exception as exc:  # noqa: BLE001 - settle the job, keep serving
            logger.exception("corpus job %s failed", job_id)
            with session_scope() as session:
                job = session.get(repo.PlagCorpusJob, job_id)
                if job is not None:
                    repo.finish_corpus_job(
                        session,
                        job,
                        status=CorpusJobStatus.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        backoff_seconds=self.settings.plag_backoff_base,
                    )
        return True

    def _run_check(self) -> bool:
        with session_scope() as session:
            check = repo.claim_check(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.plag_lease_seconds,
            )
            if check is None:
                return False
            check_id = check.id
            tenant_id = check.tenant_id

        try:
            with session_scope() as session:
                result = CheckRunner(self.settings).run(session, check_id)
                check = session.get(repo.PlagCheck, check_id)
                self._settle(session, check, result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("check %s failed", check_id)
            with session_scope() as session:
                check = session.get(repo.PlagCheck, check_id)
                if check is not None:
                    self._fail(session, check, tenant_id, exc)
        return True

    # --- settlement -----------------------------------------------------

    def _settle(self, session, check, result: dict) -> None:
        """Write the terminal status and its event in one transaction.

        Same transaction on purpose: if the status committed without the event,
        a subscriber would see a finished check whose stream never closes.
        """
        current = CheckStatus(check.status)
        if check.cancel_requested:
            target = CheckStatus.CANCELLED
        elif result.get("coverage_reason"):
            target = CheckStatus.COMPLETED_PARTIAL
        else:
            target = CheckStatus.COMPLETED

        check.status = str(advance_check(current, target))
        check.finished_at = repo.utcnow()
        check.leased_by = ""
        check.lease_expires_at = None

        if target == CheckStatus.CANCELLED:
            repo.purge_sensitive_content(session, check_id=check.id)

        repo.append_event(
            session,
            check_id=check.id,
            tenant_id=check.tenant_id,
            stage=_STAGE_FOR[target],
            status=target,
            progress=1.0,
            detail={
                "checked_chunks": result.get("checked_chunks", 0),
                "total_chunks": result.get("total_chunks", 0),
                "sources": result.get("sources", 0),
                "coverage_reason": result.get("coverage_reason") or None,
            },
        )

    def _fail(self, session, check, tenant_id: str, exc: Exception) -> None:
        """Retry while attempts remain; otherwise park in failed."""
        message = f"{type(exc).__name__}: {exc}"
        check.last_error = message[:2000]
        check.leased_by = ""
        check.lease_expires_at = None

        if check.attempts < check.max_attempts:
            check.status = str(CheckStatus.PENDING)
            check.available_at = repo.utcnow() + _backoff(self.settings, check.attempts)
            return

        check.status = str(CheckStatus.FAILED)
        check.finished_at = repo.utcnow()
        repo.append_event(
            session,
            check_id=check.id,
            tenant_id=tenant_id,
            stage=_STAGE_FOR[CheckStatus.FAILED],
            status=CheckStatus.FAILED,
            progress=1.0,
            # Never the stack trace - events are readable by the caller.
            detail={"error": type(exc).__name__},
        )


def _backoff(settings: Settings, attempts: int) -> timedelta:
    seconds = min(
        settings.plag_backoff_cap, settings.plag_backoff_base * (2 ** max(0, attempts - 1))
    )
    return timedelta(seconds=seconds)

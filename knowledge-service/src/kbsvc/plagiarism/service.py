"""PlagiarismService - the module's external seam.

HTTP routes and the CLI depend on this and nothing below it. Everything here is
about admitting work safely: corpus readiness, snapshot, idempotency, ownership
and the concurrency gate. The actual detection happens in a separate process.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .. import ids
from ..config import Settings, get_settings
from ..db.models import Document
from ..errors import KbError, NotFoundError, ValidationError
from . import repository as repo
from .models import PlagCheck, PlagCheckPassage, PlagCheckSource, utcnow
from .types import (
    CheckReport,
    CheckStage,
    CheckStatus,
    CheckSummary,
    CorpusStatus,
    CoverageReason,
    CreateDocumentCheck,
    CreateTextCheck,
    MatchedPassage,
    MatchedSource,
)

logger = logging.getLogger(__name__)


class FeatureDisabledError(KbError):
    code = "feature_disabled"
    http_status = 503


class CorpusNotReadyError(KbError):
    code = "plagiarism_corpus_not_ready"
    http_status = 409


class CorpusEmptyError(KbError):
    code = "plagiarism_corpus_empty"
    http_status = 409


class IdempotencyConflictError(KbError):
    code = "idempotency_conflict"
    http_status = 409


class ReportVisibilityChangedError(KbError):
    code = "report_visibility_changed"
    http_status = 409


class SourceVersionUnavailableError(KbError):
    """Raised by `CheckRunner._resolve_query_text` when a document-mode check's
    frozen `source_version_id` can no longer be read at detection time.

    Internal to the detection run: the worker catches this like any other run
    failure (retry while attempts remain, then `failed`). It is not part of
    the `get_report()` contract - a report only ever returns `check.query_text`
    as it was written when the check last ran, never re-derived at read time.
    """

    code = "plagiarism_source_version_unavailable"
    http_status = 409


class ConcurrencyLimitError(KbError):
    code = "plagiarism_concurrency_limit"
    http_status = 429


class InputTooLargeError(KbError):
    code = "plagiarism_input_too_large"
    http_status = 413


class CheckNotFoundError(NotFoundError):
    code = "plagiarism_check_not_found"


def _acl_allows(document_acl: list | None, allowed: set[str]) -> bool:
    """Existing ACL semantics: overlap with the caller, or explicitly public."""
    tags = set(document_acl or ["public"])
    return bool(tags & allowed) or "public" in tags


class PlagiarismService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- creation -------------------------------------------------------

    def create_text_check(self, session: Session, command: CreateTextCheck) -> CheckSummary:
        self._require_enabled()
        if len(command.text) > self.settings.plag_max_input_chars:
            raise InputTooLargeError(
                "submitted text exceeds the maximum",
                {"chars": len(command.text), "limit": self.settings.plag_max_input_chars},
            )
        if not command.text.strip():
            raise ValidationError("submitted text is empty")

        digest = repo.request_digest("text", ids.hash_text(command.text))
        return self._create(
            session,
            tenant_id=command.tenant_id,
            creator_key_id=command.creator_key_id,
            route="text",
            idempotency_key=command.idempotency_key,
            digest=digest,
            acl=command.acl,
            query_text=command.text,
            language=command.language,
        )

    def create_document_check(
        self, session: Session, command: CreateDocumentCheck
    ) -> CheckSummary:
        self._require_enabled()
        document = session.get(Document, command.document_id)
        if (
            document is None
            or document.tenant_id != command.tenant_id
            or document.deleted_at is not None
        ):
            raise CheckNotFoundError("document not found", {"document_id": command.document_id})
        if not document.current_version_id:
            raise ValidationError(
                "document has no indexed version", {"document_id": command.document_id}
            )

        digest = repo.request_digest("document", document.current_version_id)
        return self._create(
            session,
            tenant_id=command.tenant_id,
            creator_key_id=command.creator_key_id,
            route="document",
            idempotency_key=command.idempotency_key,
            digest=digest,
            acl=command.acl,
            source_document_id=document.id,
            source_version_id=document.current_version_id,
            # Every version, not just the current one - otherwise an earlier
            # revision of this same work matches itself.
            excluded_document_id=document.id,
        )

    def _create(
        self,
        session: Session,
        *,
        tenant_id: str,
        creator_key_id: str,
        route: str,
        idempotency_key: str,
        digest: str,
        acl: list[str],
        query_text: str = "",
        language: str = "auto",
        source_document_id: str = "",
        source_version_id: str = "",
        excluded_document_id: str = "",
    ) -> CheckSummary:
        """Admit one check, or explain why not. One transaction throughout."""
        existing = repo.find_by_idempotency_key(
            session,
            tenant_id=tenant_id,
            creator_key_id=creator_key_id,
            route=route,
            key=idempotency_key,
        )
        if existing is not None:
            if existing.request_digest != digest:
                raise IdempotencyConflictError(
                    "idempotency key was already used for a different request",
                    {"idempotency_key": idempotency_key},
                )
            return self._summarise(existing)

        config_hash = self.settings.plagiarism_algorithm_config_hash
        visible = self._visible_document_ids(session, tenant_id, acl)
        readiness = repo.projection_readiness(
            session,
            tenant_id=tenant_id,
            algorithm_config_hash=config_hash,
            document_ids=visible,
        )
        if readiness["total"] == 0:
            raise CorpusEmptyError("no documents are visible to this caller")
        if readiness["ready"] != readiness["total"]:
            # Running against a partly-built corpus would report "no matches"
            # for reasons that have nothing to do with the submitted text.
            raise CorpusNotReadyError("the corpus is still being built", readiness)

        # Count-and-insert must be atomic, or two simultaneous requests both see
        # room and both insert, overshooting the limit.
        repo.lock_creator(session, tenant_id=tenant_id, creator_key_id=creator_key_id)
        active = repo.count_active_checks(
            session, tenant_id=tenant_id, creator_key_id=creator_key_id
        )
        if active >= self.settings.plag_max_active_checks_per_key:
            raise ConcurrencyLimitError(
                "too many checks already running for this credential",
                {"active": active, "limit": self.settings.plag_max_active_checks_per_key},
            )

        check = PlagCheck(
            id=ids.new_id(),
            tenant_id=tenant_id,
            creator_key_id=creator_key_id,
            route=route,
            idempotency_key=idempotency_key,
            request_digest=digest,
            query_text=query_text,
            query_chars=len(query_text),
            source_document_id=source_document_id,
            source_version_id=source_version_id,
            excluded_document_id=excluded_document_id,
            language=language,
            acl=acl or ["public"],
            snapshot_at=utcnow(),
            algorithm_config_hash=config_hash,
            status=str(CheckStatus.PENDING),
            max_attempts=self.settings.plag_max_attempts,
        )
        session.add(check)
        session.flush()

        repo.append_event(
            session,
            check_id=check.id,
            tenant_id=tenant_id,
            stage=CheckStage.QUEUED,
            status=CheckStatus.PENDING,
            progress=0.0,
        )
        return self._summarise(check)

    # --- reads ----------------------------------------------------------

    def get_check(
        self, session: Session, *, check_id: str, tenant_id: str, creator_key_id: str
    ) -> CheckSummary:
        return self._summarise(self._own(session, check_id, tenant_id, creator_key_id))

    def list_checks(
        self,
        session: Session,
        *,
        tenant_id: str,
        creator_key_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CheckSummary]:
        return [
            self._summarise(check)
            for check in repo.list_checks(
                session,
                tenant_id=tenant_id,
                creator_key_id=creator_key_id,
                limit=limit,
                offset=offset,
            )
        ]

    def get_report(
        self, session: Session, *, check_id: str, tenant_id: str, creator_key_id: str
    ) -> CheckReport:
        check = self._own(session, check_id, tenant_id, creator_key_id)

        sources = (
            session.query(PlagCheckSource).filter_by(check_id=check.id).all()
        )
        # Re-check visibility now, not just at creation: access can be revoked
        # between running a check and reading its report, and a stored report
        # must not become a way around that.
        visible = set(self._visible_document_ids(session, tenant_id, check.acl or []))
        for source in sources:
            if source.document_id not in visible:
                raise ReportVisibilityChangedError(
                    "a source in this report is no longer visible to you",
                    {"check_id": check.id},
                )

        matched_sources = []
        all_spans: list[tuple[int, int]] = []
        for source in sources:
            passages = session.query(PlagCheckPassage).filter_by(source_id=source.id).all()
            all_spans.extend((p.query_start, p.query_end) for p in passages)
            matched_sources.append(
                MatchedSource(
                    document_id=source.document_id,
                    version_id=source.version_id,
                    version=source.version,
                    content_hash=source.content_hash,
                    title=source.title,
                    matched_chars=source.matched_chars,
                    score=source.score,
                    passages=[
                        MatchedPassage(
                            query_start=p.query_start,
                            query_end=p.query_end,
                            source_start=p.source_start,
                            source_end=p.source_end,
                            score=p.score,
                            preview=p.preview,
                        )
                        for p in passages
                    ],
                )
            )

        from .intervals import merge_intervals

        return CheckReport(
            check_id=check.id,
            status=CheckStatus(check.status),
            snapshot_at=check.snapshot_at,
            algorithm_config_hash=check.algorithm_config_hash,
            query_chars=check.query_chars,
            matched_chars=check.matched_chars,
            checked_chunks=check.checked_chunks,
            total_chunks=check.total_chunks,
            sources=matched_sources,
            unique_passages=merge_intervals(all_spans),
            coverage_reason=CoverageReason(check.coverage_reason)
            if check.coverage_reason
            else None,
            # Whatever CheckRunner last wrote, verbatim - never re-derived at
            # read time. New checks always have it (text mode at creation,
            # document mode at detection). A document-mode check persisted
            # before the runner started writing this snapshot degrades to an
            # empty string here rather than being backfilled.
            query_text=check.query_text or "",
        )

    def get_corpus_status(
        self, session: Session, *, tenant_id: str, acl: list[str]
    ) -> CorpusStatus:
        config_hash = self.settings.plagiarism_algorithm_config_hash
        visible = self._visible_document_ids(session, tenant_id, acl)
        readiness = repo.projection_readiness(
            session,
            tenant_id=tenant_id,
            algorithm_config_hash=config_hash,
            document_ids=visible,
        )
        return CorpusStatus(
            total_documents=readiness["total"],
            ready_documents=readiness["ready"],
            pending_documents=readiness["pending"],
            failed_documents=readiness["failed"],
            algorithm_config_hash=config_hash,
        )

    # --- deletion -------------------------------------------------------

    def delete_check(
        self, session: Session, *, check_id: str, tenant_id: str, creator_key_id: str
    ) -> str:
        """Delete or request cancellation. Returns the resulting status.

        A running check cannot be killed outright - the worker owns it. It is
        flagged instead, and the worker stops at its next checkpoint.
        """
        check = self._own(session, check_id, tenant_id, creator_key_id)
        status = CheckStatus(check.status)

        if status == CheckStatus.RUNNING:
            check.cancel_requested = 1
            check.status = str(CheckStatus.CANCEL_REQUESTED)
            session.flush()
            return str(CheckStatus.CANCEL_REQUESTED)

        if status == CheckStatus.PENDING:
            check.status = str(CheckStatus.CANCELLED)
            check.cancel_requested = 1
            repo.purge_sensitive_content(session, check_id=check.id)
            repo.append_event(
                session,
                check_id=check.id,
                tenant_id=tenant_id,
                stage=CheckStage.CANCELLED,
                status=CheckStatus.CANCELLED,
                progress=1.0,
            )
            return str(CheckStatus.CANCELLED)

        # Terminal: delete outright.
        repo.purge_sensitive_content(session, check_id=check.id)
        session.delete(check)
        session.flush()
        return "deleted"

    # --- internals ------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self.settings.plag_enabled:
            raise FeatureDisabledError("plagiarism detection is not enabled")

    def _own(
        self, session: Session, check_id: str, tenant_id: str, creator_key_id: str
    ) -> PlagCheck:
        check = repo.load_check(
            session, check_id=check_id, tenant_id=tenant_id, creator_key_id=creator_key_id
        )
        if check is None:
            # Not-yours and does-not-exist are the same answer on purpose.
            raise CheckNotFoundError("check not found", {"check_id": check_id})
        return check

    def _visible_document_ids(
        self, session: Session, tenant_id: str, acl: list[str]
    ) -> list[str]:
        """Documents this caller may compare against, by the existing ACL rules.

        One query. The ACL lives on the row, so fetching ids and then re-reading
        each document to inspect it is an N+1 - and this runs on the create path,
        which has a 500 ms P95 target. Measured at 201 documents: 815 ms P95 with
        the per-document read, and it only showed up under a non-empty ACL,
        which is every real API caller.
        """
        from sqlalchemy import select

        rows = session.execute(
            select(Document.id, Document.acl).where(
                Document.tenant_id == tenant_id,
                Document.deleted_at.is_(None),
                Document.current_version_id.is_not(None),
            )
        ).all()
        if not acl:
            return [document_id for document_id, _ in rows]

        allowed = set(acl)
        return [
            document_id
            for document_id, document_acl in rows
            if _acl_allows(document_acl, allowed)
        ]

    def _summarise(self, check: PlagCheck) -> CheckSummary:
        return CheckSummary(
            check_id=check.id,
            status=CheckStatus(check.status),
            created_at=check.created_at,
            snapshot_at=check.snapshot_at,
            algorithm_config_hash=check.algorithm_config_hash,
            source_document_id=check.source_document_id or None,
            matched_chars=check.matched_chars,
            query_chars=check.query_chars,
        )

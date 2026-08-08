"""Executes one detection check.

Chunk the query, probe for candidates, align, aggregate, persist. Cancellation
and the time budget are checked between every batch, so a check stops promptly
instead of running to the end of a long candidate list.

A run that exhausts its budget finishes as `completed_partial` with the findings
it has. That is deliberately a different terminal state from `completed`: the
caller must be able to tell "we looked at everything and this is what reuse
there is" from "we ran out of time and this is what we happened to find".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .. import ids
from ..config import Settings, get_settings
from ..db.models import Document, DocumentVersion
from . import repository as repo
from .alignment.seed_extend import AlignedPassage, align
from .chunking.sliding import chunk_document
from .fingerprinting import fingerprint
from .intervals import dedupe_passage_indexes, merge_intervals
from .language import pysbd_language
from .matching_policy import MatchPolicy, resolve_match_policy
from .models import PlagCheck, PlagCheckPassage, PlagCheckSource
from .retrieval import stop_list
from .types import CheckStage, CheckStatus, CoverageReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Candidate:
    """Where a retrieved candidate chunk sits in its source document."""

    projection_id: str
    char_start: int


@dataclass
class _Budget:
    """Wall-clock allowance, scaled to input size.

    The configured budget buys a reference number of characters; a longer input
    gets proportionally longer, so a big document is not guaranteed to time out
    while a small one coasts.
    """

    deadline: float
    cancelled: bool = False

    @classmethod
    def for_input(cls, settings: Settings, query_chars: int) -> _Budget:
        scale = max(1.0, query_chars / max(1, settings.plag_budget_reference_chars))
        return cls(deadline=time.monotonic() + settings.plag_budget_seconds * scale)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def should_stop(self) -> bool:
        return self.cancelled or self.expired


class CheckRunner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def run(self, session: Session, check_id: str) -> dict:
        """Execute `check_id`, persist its findings, and settle its status."""
        check = session.get(PlagCheck, check_id)
        if check is None:
            raise ValueError(f"check {check_id} not found")

        # A retry must not append to the previous attempt's rows, or the report
        # double-counts every passage it rediscovers.
        repo.clear_results(session, check_id=check.id)

        query_text = self._resolve_query_text(session, check)
        policy = self._policy_for_check(check, query_text)
        if policy.effective_chars < policy.min_effective_chars:
            from .service import InputTooShortError

            raise InputTooShortError(
                "有效文本少于 12 个字符，无法可靠查重",
                {"effective_chars": policy.effective_chars, "minimum": policy.min_effective_chars},
            )
        # Write the detection-time snapshot back onto the row for both modes.
        # Text mode already carries it (set at creation); document mode has
        # never had it until now - this is what lets `get_report` return it
        # without ever re-parsing object storage at read time.
        check.query_text = query_text
        check.matcher_version = policy.matcher_version
        check.matcher_config = policy.to_config()
        budget = _Budget.for_input(self.settings, len(query_text))

        self._emit(session, check, CheckStage.STARTED, progress=0.0)

        chunks = (
            chunk_document(
                query_text,
                sentences_per_chunk=self.settings.plag_sentences_per_chunk,
                overlap=self.settings.plag_chunk_overlap,
                language=pysbd_language(check.language)
                if check.language != "auto"
                else self._detect(query_text),
            )
            if query_text.strip()
            else []
        )
        check.total_chunks = len(chunks)
        check.query_chars = len(query_text)
        self._emit(session, check, CheckStage.CHUNKING, progress=0.05)

        projection_ids = repo.active_projection_ids(
            session,
            tenant_id=check.tenant_id,
            algorithm_config_hash=check.algorithm_config_hash,
            snapshot_at=check.snapshot_at,
        )
        corpus_size = repo.live_projection_count(
            session,
            tenant_id=check.tenant_id,
            algorithm_config_hash=check.algorithm_config_hash,
        )
        stopped = stop_list.stop_fingerprints(
            session,
            tenant_id=check.tenant_id,
            algorithm_config_hash=check.algorithm_config_hash,
            ratio_threshold=self.settings.plag_df_ratio_threshold,
            corpus_size=corpus_size,
        )

        passages_by_projection: dict[str, list[AlignedPassage]] = {}
        # Candidate chunk id -> where that chunk sits in its source document.
        candidate_index: dict[str, _Candidate] = {}
        checked = 0

        for chunk in chunks:
            # Re-read the cancel flag: it is set by another process.
            session.refresh(check, ["cancel_requested"])
            budget.cancelled = bool(check.cancel_requested)
            if budget.should_stop():
                break

            # A rule line or separator run matches every other one in the corpus
            # exactly, at score 1.0 - real verbatim matches, and meaningless.
            # Counted as checked: it *was* examined, and excluding it would make
            # the coverage numbers lie.
            if not stop_list.is_discriminative(
                chunk.text, min_word_ratio=self.settings.plag_min_word_ratio
            ):
                checked += 1
                continue

            raw_probe = fingerprint(
                chunk.text,
                k=self.settings.plag_kgram,
                w=self.settings.plag_winnow_window,
                profile=policy.profile,
            )
            # A 12–19-character Chinese query has only one strict exact
            # opportunity; removing its sole fingerprints via DF stop-list
            # would turn a real match into a misleading clean report.
            probe = raw_probe if policy.short_exact_enabled else stop_list.apply(raw_probe, stopped)
            candidates = repo.find_candidate_chunks(
                session,
                tenant_id=check.tenant_id,
                projection_ids=projection_ids,
                fingerprints=probe,
                limit=self.settings.plag_candidate_top_k,
                excluded_document_id=check.excluded_document_id,
            )
            for candidate_id, _, projection_id, char_start in candidates:
                candidate_index[candidate_id] = _Candidate(projection_id, char_start)

            for passage in align(
                str(chunk.chunk_index),
                chunk.text,
                [(cid, ctext) for cid, ctext, _, _ in candidates],
                min_seed_len=policy.min_seed_len,
                min_passage_len=policy.min_passage_len,
                extend_tolerance=self.settings.plag_extend_tolerance,
                should_stop=budget.should_stop,
                normalizer_profile=policy.profile,
            ):
                # Guard the emitted span, not just the probe chunk. A rule line
                # embedded in otherwise real prose rides through the chunk-level
                # check - the chunk is mostly words - and then aligns against
                # every other rule line in the corpus at score 1.0. What has to
                # be discriminative is the span actually being reported.
                matched_text = chunk.text[passage.query_start : passage.query_end]
                if not stop_list.is_discriminative(
                    matched_text, min_word_ratio=self.settings.plag_min_word_ratio
                ):
                    continue

                source = candidate_index[passage.candidate_chunk_id]
                # Translate chunk-local offsets into document coordinates on
                # both sides, so a stored finding needs no further context to be
                # resolved later.
                passages_by_projection.setdefault(source.projection_id, []).append(
                    AlignedPassage(
                        query_chunk_id=passage.query_chunk_id,
                        candidate_chunk_id=passage.candidate_chunk_id,
                        query_start=chunk.char_start + passage.query_start,
                        query_end=chunk.char_start + passage.query_end,
                        candidate_start=source.char_start + passage.candidate_start,
                        candidate_end=source.char_start + passage.candidate_end,
                        score=passage.score,
                    )
                )

            checked += 1
            if checked % 5 == 0 or checked == len(chunks):
                self._emit(
                    session,
                    check,
                    CheckStage.RETRIEVING,
                    progress=0.05 + 0.75 * (checked / max(1, len(chunks))),
                    detail={"checked_chunks": checked, "total_chunks": len(chunks)},
                )

        check.checked_chunks = checked
        self._emit(session, check, CheckStage.PERSISTING, progress=0.9)
        matched = self._persist(session, check, passages_by_projection, query_text)

        coverage = None
        if budget.cancelled:
            coverage = CoverageReason.CANCELLED
        elif checked < len(chunks):
            coverage = CoverageReason.TIME_CAP
        check.coverage_reason = str(coverage) if coverage else ""
        check.matched_chars = matched

        return {
            "checked_chunks": checked,
            "total_chunks": len(chunks),
            "matched_chars": matched,
            "coverage_reason": check.coverage_reason,
            "sources": len(passages_by_projection),
        }

    # --- helpers --------------------------------------------------------

    def _policy_for_check(self, check: PlagCheck, query_text: str) -> MatchPolicy:
        """Use the policy frozen at admission, including on retries."""
        if check.matcher_config:
            try:
                return MatchPolicy.from_config(check.matcher_config)
            except (KeyError, TypeError, ValueError):
                logger.warning("discarding malformed matcher_config for check %s", check.id)
        return resolve_match_policy(query_text, check.language, self.settings)

    def _detect(self, text: str) -> str:
        from .language import detect_language

        return pysbd_language(detect_language(text))

    def _resolve_query_text(self, session: Session, check: PlagCheck) -> str:
        """Text mode carries its own text; document mode reads the frozen version.

        This is the one place the source document is re-read to answer "what is
        actually being checked" - the caller writes the result back onto
        `check.query_text`, so later report reads never repeat this resolution.
        A missing frozen version is a hard failure here, not a silent empty
        result: an empty query would make the run "complete" with zero chunks
        and zero matches, which reads exactly like a legitimately empty
        document instead of the unresolvable one it actually is.
        """
        if check.query_text:
            return check.query_text
        if not check.source_version_id:
            return ""
        from .projection import ProjectionBuilder
        from .service import SourceVersionUnavailableError

        version = session.get(DocumentVersion, check.source_version_id)
        if version is None:
            raise SourceVersionUnavailableError(
                "the frozen source version for this check no longer exists",
                {"check_id": check.id, "source_version_id": check.source_version_id},
            )
        return ProjectionBuilder(self.settings)._load_text(session, version)

    def _persist(
        self,
        session: Session,
        check: PlagCheck,
        passages_by_projection: dict[str, list[AlignedPassage]],
        query_text: str,
    ) -> int:
        """Freeze findings against the source version they were found in."""
        all_query_spans: list[tuple[int, int]] = []

        for projection_id, passages in passages_by_projection.items():
            projection = session.get(repo.PlagCorpusProjection, projection_id)
            if projection is None:
                continue
            keep = dedupe_passage_indexes(
                [
                    (p.query_start, p.query_end, p.candidate_start, p.candidate_end)
                    for p in passages
                ],
                [p.score for p in passages],
            )
            passages = [passages[index] for index in keep]
            if not passages:
                continue
            document = session.get(Document, projection.document_id)
            # The real ordinal, not a placeholder: Task 7 fetches source
            # excerpts against this exact frozen version rather than whatever
            # the document looks like today, and `0` is never a real version.
            # `projection.version_id` is a straight primary-key lookup - the
            # projection itself already carries the reference, so there is no
            # need to add a column or backfill anything to answer this.
            version = session.get(DocumentVersion, projection.version_id)
            if version is None:
                raise RuntimeError(
                    f"projection {projection.id} references missing version "
                    f"{projection.version_id}"
                )

            spans = [(p.query_start, p.query_end) for p in passages]
            merged = merge_intervals(spans)
            matched_chars = sum(end - start for start, end in merged)
            all_query_spans.extend(spans)

            source = PlagCheckSource(
                id=ids.new_id(),
                check_id=check.id,
                tenant_id=check.tenant_id,
                document_id=projection.document_id,
                version_id=projection.version_id,
                version=version.version,
                content_hash=projection.content_hash,
                title=(document.title if document else "")[:512],
                matched_chars=matched_chars,
                score=max((p.score for p in passages), default=0.0),
            )
            session.add(source)
            session.flush()

            limit = self.settings.plag_preview_chars
            session.add_all(
                [
                    PlagCheckPassage(
                        id=ids.new_id(),
                        check_id=check.id,
                        source_id=source.id,
                        query_start=p.query_start,
                        query_end=p.query_end,
                        source_start=p.candidate_start,
                        source_end=p.candidate_end,
                        score=p.score,
                        # Capped: a report must not become a way to read source
                        # documents around the ACL check.
                        preview=query_text[p.query_start : p.query_end][:limit],
                    )
                    for p in passages
                ]
            )

        session.flush()
        # Dedupe across sources - a passage reused from two works is one region
        # of the query, not two.
        return sum(end - start for start, end in merge_intervals(all_query_spans))

    def _emit(
        self,
        session: Session,
        check: PlagCheck,
        stage: CheckStage,
        *,
        progress: float,
        detail: dict | None = None,
    ) -> None:
        repo.append_event(
            session,
            check_id=check.id,
            tenant_id=check.tenant_id,
            stage=stage,
            status=CheckStatus(check.status),
            progress=round(progress, 4),
            detail=detail or {},
        )

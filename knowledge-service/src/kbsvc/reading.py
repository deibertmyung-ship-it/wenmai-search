"""Historical document passage location for the reader deep-link."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access import document_is_visible
from .db import repo
from .db.models import Chunk, Document, DocumentVersion
from .errors import (
    InvalidPassageRangeError,
    NotFoundError,
    PassageLocationUnavailableError,
    VersionNotFoundError,
)

MAX_CONTEXT_CHUNKS = 10
MAX_WINDOW_CHUNKS = 200
MAX_PASSAGE_CHARS = 200_000


@dataclass(frozen=True)
class ChunkSpan:
    ordinal: int
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class HighlightRange:
    local_start: int
    local_end: int
    document_start: int
    document_end: int


@dataclass(frozen=True)
class HighlightAllocation:
    by_ordinal: dict[int, tuple[HighlightRange, ...]]
    uncovered: tuple[tuple[int, int], ...]
    projection_gaps: tuple[tuple[int, int], ...] = ()

    @property
    def exact(self) -> bool:
        return not self.uncovered and bool(self.by_ordinal)


@dataclass(frozen=True)
class PassageChunk:
    chunk: Chunk
    highlights: tuple[HighlightRange, ...]


@dataclass(frozen=True)
class PassageWindow:
    document_id: str
    version_id: str
    version: int
    requested_start: int
    requested_end: int
    focus_ordinal: int
    from_ordinal: int
    next_from: int
    has_more: bool
    chunks: tuple[PassageChunk, ...]


def _subtract_interval(
    start: int, end: int, covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    pieces = [(start, end)]
    for covered_start, covered_end in covered:
        next_pieces: list[tuple[int, int]] = []
        for piece_start, piece_end in pieces:
            if covered_end <= piece_start or covered_start >= piece_end:
                next_pieces.append((piece_start, piece_end))
                continue
            if piece_start < covered_start:
                next_pieces.append((piece_start, min(covered_start, piece_end)))
            if covered_end < piece_end:
                next_pieces.append((max(covered_end, piece_start), piece_end))
        pieces = next_pieces
    return [
        (piece_start, piece_end)
        for piece_start, piece_end in pieces
        if piece_start < piece_end
    ]


def _merge_interval(covered: list[tuple[int, int]], start: int, end: int) -> None:
    covered.append((start, end))
    covered.sort()
    merged: list[tuple[int, int]] = []
    for item_start, item_end in covered:
        if not merged or item_start > merged[-1][1]:
            merged.append((item_start, item_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], item_end))
    covered[:] = merged


def resolve_highlights(
    chunks: list[ChunkSpan] | tuple[ChunkSpan, ...], *, start: int, end: int
) -> HighlightAllocation:
    """Allocate a source range to overlapping chunks exactly once."""

    if start < 0 or end <= start:
        raise ValueError("range must be a non-empty half-open interval")

    ordered = sorted(chunks, key=lambda item: (item.ordinal, item.char_start))
    covered: list[tuple[int, int]] = []
    by_ordinal: dict[int, tuple[HighlightRange, ...]] = {}

    for chunk in ordered:
        if chunk.char_start < 0 or chunk.char_end != chunk.char_start + len(chunk.text):
            continue
        intersection_start = max(start, chunk.char_start)
        intersection_end = min(end, chunk.char_end)
        if intersection_start >= intersection_end:
            continue
        pieces = _subtract_interval(intersection_start, intersection_end, covered)
        if not pieces:
            continue
        ranges = tuple(
            HighlightRange(
                local_start=piece_start - chunk.char_start,
                local_end=piece_end - chunk.char_start,
                document_start=piece_start,
                document_end=piece_end,
            )
            for piece_start, piece_end in pieces
        )
        by_ordinal[chunk.ordinal] = ranges
        for piece_start, piece_end in pieces:
            _merge_interval(covered, piece_start, piece_end)

    uncovered = _subtract_interval(start, end, covered)
    # ProjectionBuilder pads gaps between adjacent knowledge-base chunks with
    # spaces to preserve absolute offsets.  Those synthetic characters have
    # no source chunk to mark, but are explainable when both sides are valid
    # neighbouring chunks.  Keep them separate from genuinely missing data.
    known_gaps = [
        (previous.char_end, current.char_start)
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if (
            previous.char_start >= 0
            and previous.char_end == previous.char_start + len(previous.text)
            and current.char_start >= 0
            and current.char_end == current.char_start + len(current.text)
            and previous.ordinal + 1 == current.ordinal
            and previous.char_end < current.char_start
        )
    ]
    explainable = [
        interval
        for interval in uncovered
        if any(
            gap_start <= interval[0] and interval[1] <= gap_end
            for gap_start, gap_end in known_gaps
        )
    ]
    unresolved = tuple(interval for interval in uncovered if interval not in explainable)
    return HighlightAllocation(
        by_ordinal=by_ordinal,
        uncovered=unresolved,
        projection_gaps=tuple(explainable),
    )


def _validate_range(start: int, end: int, context: int) -> None:
    if start < 0 or end <= start or end - start > MAX_PASSAGE_CHARS:
        raise InvalidPassageRangeError(
            "passage range must be a bounded non-empty half-open interval",
            {"start": start, "end": end, "max_chars": MAX_PASSAGE_CHARS},
        )
    if context < 0 or context > MAX_CONTEXT_CHUNKS:
        raise InvalidPassageRangeError(
            "context is outside the supported range",
            {"context": context, "minimum": 0, "maximum": MAX_CONTEXT_CHUNKS},
        )


class PassageLocator:
    def locate(
        self,
        session: Session,
        *,
        tenant_id: str,
        document_id: str,
        version: int,
        start: int,
        end: int,
        context: int = 2,
        allowed_acl: set[str] | list[str] | None = None,
    ) -> PassageWindow:
        _validate_range(start, end, context)
        document = session.get(Document, document_id)
        if (
            document is None
            or document.tenant_id != tenant_id
            or document.deleted_at is not None
            or not document_is_visible(document.acl, allowed_acl)
        ):
            raise NotFoundError("document not found", {"document_id": document_id})

        version_row = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == version,
            )
        ).first()
        if version_row is None:
            raise VersionNotFoundError(
                "version not found", {"document_id": document_id, "version": version}
            )

        matching = repo.find_chunks_overlapping(
            session,
            version_id=version_row.id,
            start=start,
            end=end,
            limit=MAX_WINDOW_CHUNKS + 1,
        )
        if len(matching) > MAX_WINDOW_CHUNKS:
            raise PassageLocationUnavailableError("passage spans too many chunks", {})

        allocation = resolve_highlights(
            tuple(
                ChunkSpan(
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                )
                for chunk in matching
            ),
            start=start,
            end=end,
        )
        if not allocation.exact:
            raise PassageLocationUnavailableError(
                "stored chunk offsets cannot resolve this passage",
                {"document_id": document_id, "version": version},
            )

        focus_ordinal = min(allocation.by_ordinal)
        first_ordinal = max(0, focus_ordinal - context)
        last_ordinal = max(allocation.by_ordinal) + context
        window = repo.fetch_chunk_ordinal_window(
            session,
            version_id=version_row.id,
            first_ordinal=first_ordinal,
            last_ordinal=last_ordinal,
        )
        chunks = tuple(
            PassageChunk(chunk=chunk, highlights=allocation.by_ordinal.get(chunk.ordinal, ()))
            for chunk in window
        )
        if not chunks:
            raise PassageLocationUnavailableError("passage window is empty", {})
        return PassageWindow(
            document_id=document.id,
            version_id=version_row.id,
            version=version_row.version,
            requested_start=start,
            requested_end=end,
            focus_ordinal=focus_ordinal,
            from_ordinal=window[0].ordinal,
            next_from=window[-1].ordinal + 1,
            has_more=repo.has_chunks_after(
                session, version_id=version_row.id, ordinal=window[-1].ordinal
            ),
            chunks=chunks,
        )

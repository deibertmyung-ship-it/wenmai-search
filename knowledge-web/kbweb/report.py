"""Report assembly for plagiarism checks.

Pure data shaping between what kbsvc returns and what the templates need.
Kept out of the view so the numbering, span and excerpt logic can be tested
without a request context.
"""

from __future__ import annotations

import logging

from .errors import BackendError, BackendUnavailable

logger = logging.getLogger(__name__)

MAX_PREFETCHED_SOURCES = 8
SOURCE_CHUNK_PAGE_SIZE = 200
SOURCE_MAX_PAGES = 20


def source_excerpt(chunks: list[dict], start: int, end: int) -> str:
    """Stitch the source text covering `[start, end)` out of chunks.

    Offsets are in *document* coordinates while `text` is per chunk, so every
    slice is clamped to the fragment actually in hand: a chunk whose declared
    `char_end` disagrees with `len(text)` (normalisation, re-parse) must clamp
    rather than raise. A gap between chunks simply contributes nothing.
    """
    parts: list[str] = []
    for chunk in chunks:
        chunk_start = chunk.get("char_start") or 0
        text = chunk.get("text") or ""
        chunk_end = chunk_start + len(text)
        if chunk_end <= start or chunk_start >= end:
            continue
        left = max(0, start - chunk_start)
        right = min(len(text), end - chunk_start)
        if left < right:
            parts.append(text[left:right])
    return "".join(parts)


def numbered_sources(report: dict) -> list[dict]:
    """Sources in display order, each carrying its 1-based `ordinal`.

    The ordinal is the only thing tying a marker in the text to a row in the
    list, so ordering and numbering must be decided in one place - which is
    here, not in the template.
    """
    ordered = sorted(
        report.get("sources") or [],
        key=lambda source: source.get("matched_chars") or 0,
        reverse=True,
    )
    return [dict(source, ordinal=index) for index, source in enumerate(ordered, start=1)]


def duplication_ratio(report: dict) -> float:
    """Matched share of the submission, as a percentage.

    Callers must pair this with `is_complete`: when coverage stopped early the
    number is a floor, not a verdict.
    """
    query_chars = report.get("query_chars") or 0
    if query_chars <= 0:
        return 0.0
    return round((report.get("matched_chars") or 0) / query_chars * 100, 1)


def query_spans(sources: list[dict]) -> list[tuple[int, int, int]]:
    """Flatten numbered sources into `(start, end, ordinal)` for coverage_segments.

    Requires sources already carrying `ordinal` - see `numbered_sources`.
    """
    return [
        (passage["query_start"], passage["query_end"], source["ordinal"])
        for source in sources
        for passage in source.get("passages") or []
    ]


def _source_chunks(
    api, source: dict, passages: list[dict]
) -> tuple[list[dict], bool]:
    """Read the frozen version until all requested offsets are covered or capped."""
    if not passages:
        return [], True
    target_end = max(
        (passage.get("source_end") or 0 for passage in passages), default=0
    )
    chunks: list[dict] = []
    from_ordinal = 0
    covered = target_end <= 0
    for _ in range(SOURCE_MAX_PAGES):
        page = api.get_chunks(
            source["document_id"],
            from_ordinal=from_ordinal,
            limit=SOURCE_CHUNK_PAGE_SIZE,
            version=source["version"],
        )
        if not page:
            break
        chunks.extend(page)
        covered = (
            max((chunk.get("char_end") or 0 for chunk in page), default=0)
            >= target_end
        )
        if covered or len(page) < SOURCE_CHUNK_PAGE_SIZE:
            break
        next_ordinal = (
            max((chunk.get("ordinal") or 0 for chunk in page), default=-1) + 1
        )
        if next_ordinal <= from_ordinal:
            break
        from_ordinal = next_ordinal
    return chunks, covered


def attach_excerpts(
    api, sources: list[dict], *, limit: int = MAX_PREFETCHED_SOURCES
) -> list[dict]:
    """Give each passage of the top `limit` sources its source-side text.

    Bounded on purpose: never one request per passage. At most `limit` source
    documents are prefetched, each in pages of <= 200 chunks and with a hard
    page cap. The rest keep a link into the reader instead.

    A source that has become unreachable degrades to an empty excerpt. Access
    can be revoked between running a check and reading its report, and one
    deleted book must not take the whole report down with it.
    """
    attached: list[dict] = []
    for index, source in enumerate(sources):
        passages = source.get("passages") or []
        if index >= limit:
            attached.append(dict(source, prefetched=False))
            continue

        try:
            chunks, covered = _source_chunks(api, source, passages)
        except BackendError as exc:
            if exc.status not in {403, 404}:
                raise
            logger.info(
                "source text unavailable for %s: %s",
                source["document_id"],
                exc,
            )
            attached.append(
                dict(source, prefetched=True, fetch_error="来源已不可访问。")
            )
            continue
        except BackendUnavailable as exc:
            logger.info(
                "source service unavailable for %s: %s",
                source["document_id"],
                exc,
            )
            attached.append(
                dict(source, prefetched=True, fetch_error="来源服务暂时不可用。")
            )
            continue

        attached.append(
            dict(
                source,
                prefetched=True,
                prefetch_truncated=not covered,
                passages=[
                    dict(
                        passage,
                        source_text=source_excerpt(
                            chunks,
                            passage["source_start"],
                            passage["source_end"],
                        ),
                    )
                    for passage in passages
                ],
            )
        )
    return attached

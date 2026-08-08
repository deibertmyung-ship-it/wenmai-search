"""Report assembly for plagiarism checks.

Pure data shaping between what kbsvc returns and what the templates need.
Kept out of the view so the numbering, span and excerpt logic can be tested
without a request context.
"""

from __future__ import annotations


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

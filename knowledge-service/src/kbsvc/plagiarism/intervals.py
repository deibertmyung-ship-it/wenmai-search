"""Shared interval-merge helper.

Intervals are half-open: `[start, end)`. Adjacent intervals where one starts
exactly where the previous ends *do* merge - they describe a contiguous run of
covered query characters.

Ported from noplag-engine `src/noplag_engine/intervals.py` at commit
005da60faad21bf52702997d73583b78d8905d22, Apache-2.0 (see
`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`).
Modified for kbsvc: docstring trimmed to drop references to upstream modules
that were not ported. Algorithm unchanged.
"""

from __future__ import annotations

_DUPLICATE_OVERLAP_RATIO = 0.95


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjoining half-open `(start, end)` intervals.

    Sorts by `(start, end)` then folds left-to-right, extending the last-kept
    interval whenever the next one starts at or before the last `end`. Returns
    a fresh list; input is not mutated.
    """
    if not intervals:
        return []
    sorted_iv = sorted(intervals)
    merged: list[tuple[int, int]] = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def dedupe_passage_indexes(
    passages: list[tuple[int, int, int, int]],
    scores: list[float] | None = None,
) -> list[int]:
    """Return indexes for distinct physical matches within one source.

    Each tuple is ``(query_start, query_end, source_start, source_end)`` in
    document-global coordinates. Overlapping corpus chunks can report a broad
    match and a nested suffix for the same source location. Those are one
    attribution, but the same query text at two disjoint source locations is
    two genuine matches and must remain visible.

    Callers group passages by source projection before invoking this helper.
    When scores are supplied, the highest-scoring representative is kept for
    each physical match. The returned indexes preserve the input order.
    """
    if not passages:
        return []
    if scores is not None and len(scores) != len(passages):
        raise ValueError("scores must have one value per passage")

    ordered = sorted(
        range(len(passages)),
        key=lambda index: (
            passages[index][0],
            -(passages[index][1] - passages[index][0]),
            passages[index][2],
            -(passages[index][3] - passages[index][2]),
        ),
    )
    kept: list[int] = []
    for index in ordered:
        duplicate = next(
            (
                position
                for position, other in enumerate(kept)
                if _same_physical_passage(passages[index], passages[other])
            ),
            None,
        )
        if duplicate is not None:
            if scores is not None and scores[index] > scores[kept[duplicate]]:
                kept[duplicate] = index
            continue
        kept.append(index)
    return sorted(kept)


def _same_physical_passage(
    left: tuple[int, int, int, int], right: tuple[int, int, int, int]
) -> bool:
    left_query_start, left_query_end, left_source_start, left_source_end = left
    right_query_start, right_query_end, right_source_start, right_source_end = right
    left_query_len = left_query_end - left_query_start
    right_query_len = right_query_end - right_query_start
    left_source_len = left_source_end - left_source_start
    right_source_len = right_source_end - right_source_start
    if min(left_query_len, right_query_len, left_source_len, right_source_len) <= 0:
        return False

    query_overlap = max(
        0, min(left_query_end, right_query_end) - max(left_query_start, right_query_start)
    )
    source_overlap = max(
        0, min(left_source_end, right_source_end) - max(left_source_start, right_source_start)
    )
    query_ratio = query_overlap / min(left_query_len, right_query_len)
    source_ratio = source_overlap / min(left_source_len, right_source_len)
    left_diagonal = left_source_start - left_query_start
    right_diagonal = right_source_start - right_query_start
    return (
        query_ratio >= _DUPLICATE_OVERLAP_RATIO
        and source_ratio >= _DUPLICATE_OVERLAP_RATIO
        and left_diagonal == right_diagonal
    )

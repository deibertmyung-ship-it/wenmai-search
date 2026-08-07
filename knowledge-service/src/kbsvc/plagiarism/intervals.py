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

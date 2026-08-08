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

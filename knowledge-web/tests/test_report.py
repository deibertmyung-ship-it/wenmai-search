"""Report assembly. Pure data shaping, no Flask involved."""

from __future__ import annotations

from kbweb.report import source_excerpt


def chunk(ordinal: int, text: str, char_start: int) -> dict:
    return {
        "ordinal": ordinal,
        "text": text,
        "char_start": char_start,
        "char_end": char_start + len(text),
    }


def test_excerpt_slices_within_a_single_chunk():
    chunks = [chunk(0, "零一二三四五六七八九", 0)]
    assert source_excerpt(chunks, 2, 5) == "二三四"


def test_excerpt_stitches_across_chunk_boundaries():
    chunks = [chunk(0, "零一二三四", 0), chunk(1, "五六七八九", 5)]
    assert source_excerpt(chunks, 3, 7) == "三四五六"


def test_excerpt_ignores_chunks_outside_the_range():
    chunks = [chunk(0, "零一二", 0), chunk(1, "三四五", 3), chunk(2, "六七八", 6)]
    assert source_excerpt(chunks, 3, 6) == "三四五"


def test_excerpt_survives_offsets_that_do_not_match_chunk_text_length():
    """Offsets come from the document, chunk text may have been normalised.

    A mismatch must clamp, never raise and never read past the fragment.
    """
    odd = {"ordinal": 0, "text": "零一二", "char_start": 0, "char_end": 999}
    assert source_excerpt([odd], 0, 999) == "零一二"


def test_excerpt_returns_empty_when_nothing_covers_the_range():
    assert source_excerpt([chunk(0, "零一二", 0)], 50, 60) == ""
    assert source_excerpt([], 0, 10) == ""

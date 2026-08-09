"""RED tests for source-passage location and overlap-safe highlighting."""

from __future__ import annotations

import pytest

from kbsvc.reading import ChunkSpan, HighlightRange, resolve_highlights


def span(ordinal: int, text: str, start: int) -> ChunkSpan:
    return ChunkSpan(
        ordinal=ordinal,
        text=text,
        char_start=start,
        char_end=start + len(text),
    )


def test_single_chunk_returns_local_half_open_highlight() -> None:
    chunks = [span(4, "0123456789", 100)]

    result = resolve_highlights(chunks, start=103, end=107)

    assert result.exact is True
    assert result.uncovered == ()
    assert result.by_ordinal == {
        4: (HighlightRange(local_start=3, local_end=7, document_start=103, document_end=107),)
    }


def test_overlapping_chunks_assign_each_source_character_once() -> None:
    chunks = [
        span(4, "abcdefghij", 0),
        span(5, "ijKLMNOPQR", 8),
    ]

    result = resolve_highlights(chunks, start=6, end=14)

    assert result.exact is True
    assert result.by_ordinal[4] == (
        HighlightRange(local_start=6, local_end=10, document_start=6, document_end=10),
    )
    assert result.by_ordinal[5] == (
        HighlightRange(local_start=2, local_end=6, document_start=10, document_end=14),
    )


def test_invalid_range_is_rejected_before_database_lookup() -> None:
    with pytest.raises(ValueError, match="half-open"):
        resolve_highlights([span(1, "abc", 0)], start=2, end=2)


def test_coordinate_length_mismatch_is_not_clamped() -> None:
    bad = ChunkSpan(ordinal=1, text="abc", char_start=0, char_end=99)

    result = resolve_highlights([bad], start=0, end=3)

    assert result.exact is False
    assert result.by_ordinal == {}
    assert result.uncovered == ((0, 3),)


def test_known_projection_gap_is_not_treated_as_missing_source() -> None:
    chunks = [span(4, "abcd", 0), span(5, "efgh", 6)]

    result = resolve_highlights(chunks, start=2, end=8)

    assert result.exact is True
    assert result.uncovered == ()
    assert result.projection_gaps == ((4, 6),)
    assert result.by_ordinal[4][0].local_start == 2
    assert result.by_ordinal[5][0].local_end == 2


def test_passage_entirely_inside_projection_gap_is_unavailable() -> None:
    chunks = [span(4, "abcd", 0), span(5, "efgh", 6)]

    result = resolve_highlights(chunks, start=4, end=6)

    assert result.exact is False
    assert result.by_ordinal == {}

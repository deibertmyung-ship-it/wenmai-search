"""Migration baseline for the sliding chunker and the interval merge.

Vectors come from noplag-engine's `tests/test_chunking.py` and
`tests/test_intervals.py` at commit 005da60faad21bf52702997d73583b78d8905d22
(Apache-2.0). Like the winnowing baseline, these pin algorithm output on
purpose.
"""

from __future__ import annotations

import pytest

from kbsvc.plagiarism.chunking.sliding import chunk_document
from kbsvc.plagiarism.intervals import merge_intervals

pysbd = pytest.importorskip(
    "pysbd", reason="chunking needs the `plagiarism` extra: pip install '.[plagiarism]'"
)

# Upstream carried these as parameter defaults; the port moved them to settings.
PER_CHUNK = 4
OVERLAP = 1


def chunk(text: str, per_chunk: int = PER_CHUNK, overlap: int = OVERLAP):
    return chunk_document(text, sentences_per_chunk=per_chunk, overlap=overlap)


# --- sliding chunker ----------------------------------------------------


def test_empty_text_returns_empty():
    assert chunk("") == []


def test_single_sentence_yields_one_chunk():
    text = "The quick brown fox jumps over the lazy dog."
    chunks = chunk(text)
    assert len(chunks) == 1
    assert chunks[0].sentence_count == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].text == text


def test_fewer_than_sentences_per_chunk_yields_one_chunk():
    chunks = chunk("First sentence. Second sentence. Third sentence.")
    assert len(chunks) == 1
    assert chunks[0].sentence_count == 3


def test_multi_chunk_with_overlap_correctness():
    # 10 sentences at 4-per-chunk / 1-overlap (stride 3) -> [0:4], [3:7], [6:10]
    text = " ".join(f"Sentence number {i}." for i in range(10))
    chunks = chunk(text)
    assert len(chunks) == 3
    assert [c.sentence_count for c in chunks] == [4, 4, 4]
    for i in range(1, len(chunks)):
        assert chunks[i].char_start >= chunks[i - 1].char_start
        assert chunks[i].chunk_index == i


def test_char_offsets_slice_back_to_the_original_text():
    """The offsets are what a finding is reported against - if they drift, every
    highlighted span in every report is wrong."""
    text = (
        "First sentence with some words. Second sentence is here. "
        "Third sentence follows. Fourth one. Fifth sentence wraps up. "
        "Sixth and final sentence."
    )
    for c in chunk(text):
        assert c.text == text[c.char_start : c.char_end]


def test_adjacent_chunks_share_overlap_sentences():
    text = " ".join(f"Marker {i} content here." for i in range(8))
    chunks = chunk(text)
    assert len(chunks) == 3
    assert "Marker 3" in chunks[0].text and "Marker 3" in chunks[1].text
    assert "Marker 6" in chunks[1].text and "Marker 6" in chunks[2].text
    assert "Marker 0" not in chunks[1].text
    assert "Marker 1" not in chunks[1].text


def test_overlap_not_less_than_window_is_rejected():
    """A window that cannot advance would loop forever or emit duplicates."""
    with pytest.raises(ValueError):
        chunk("First. Second. Third.", per_chunk=4, overlap=4)


def test_chinese_text_chunks_with_offsets_intact():
    """Not an upstream vector - upstream's baseline is entirely Latin, and pysbd
    segments CJK by different rules."""
    text = (
        "贼克者，取用之首法也。上克下为贼。下贼上为克。"
        "涉害者，比用不成则涉害。四课三传，各有其法。"
    )
    chunks = chunk_document(text, sentences_per_chunk=2, overlap=1, language="zh")
    assert chunks
    for c in chunks:
        assert c.text == text[c.char_start : c.char_end]


# --- interval merge -----------------------------------------------------


def test_empty_merge():
    assert merge_intervals([]) == []


def test_single_passthrough():
    assert merge_intervals([(10, 20)]) == [(10, 20)]


def test_disjoint_stays_separate():
    assert merge_intervals([(0, 5), (10, 20)]) == [(0, 5), (10, 20)]


def test_overlap_merges():
    assert merge_intervals([(0, 10), (5, 15)]) == [(0, 15)]


def test_contained_absorbed():
    assert merge_intervals([(0, 20), (5, 10)]) == [(0, 20)]


def test_half_open_adjoining_merges():
    # [0,5) and [5,10) are exactly adjacent - together a contiguous run [0,10).
    assert merge_intervals([(0, 5), (5, 10)]) == [(0, 10)]


def test_out_of_order_input_handled():
    assert merge_intervals([(20, 30), (0, 10), (5, 15)]) == [(0, 15), (20, 30)]


def test_input_not_mutated():
    src = [(20, 30), (0, 10), (5, 15)]
    snapshot = list(src)
    merge_intervals(src)
    assert src == snapshot

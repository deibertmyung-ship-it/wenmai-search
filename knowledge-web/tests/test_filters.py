"""Filters carry provenance guarantees, so they get direct tests."""

from __future__ import annotations

from kbweb.filters import (
    breadcrumb,
    coverage_segments,
    filesize,
    highlight_segments,
    job_tone,
    ms,
    score,
    short_id,
    timeago,
)


def test_highlight_splits_on_backend_offsets():
    segments = highlight_segments("贼克者，取用之首法也。", [[0, 2]])
    assert segments[0] == ("贼克", True)
    assert segments[1][1] is False
    assert "".join(text for text, _ in segments) == "贼克者，取用之首法也。"


def test_highlight_without_spans_returns_plain_text():
    assert highlight_segments("abc", None) == [("abc", False)]
    assert highlight_segments("abc", []) == [("abc", False)]


def test_highlight_preserves_the_original_text_exactly():
    text = "甲乙丙丁戊己庚辛"
    for spans in ([[0, 2], [4, 6]], [[2, 4]], [[6, 8]]):
        assert "".join(part for part, _ in highlight_segments(text, spans)) == text


def test_highlight_drops_out_of_range_spans():
    """A bad payload must degrade to plain text, never scramble the passage."""
    assert highlight_segments("abc", [[0, 99]]) == [("abc", False)]
    assert highlight_segments("abc", [[-5, 2]]) == [("abc", False)]
    assert highlight_segments("abc", [[2, 1]]) == [("abc", False)]


def test_highlight_skips_overlapping_spans_rather_than_double_marking():
    segments = highlight_segments("abcdef", [[0, 3], [1, 4]])
    assert segments[0] == ("abc", True)
    assert "".join(part for part, _ in segments) == "abcdef"


def test_highlight_handles_empty_text():
    assert highlight_segments("", [[0, 1]]) == []


def test_breadcrumb_joins_or_falls_back():
    assert breadcrumb(["卷一", "总论"]) == "卷一 › 总论"
    assert breadcrumb([]) == "—"
    assert breadcrumb(None) == "—"


def test_job_tone_maps_states_to_semantic_colours():
    assert job_tone("completed") == "ok"
    assert job_tone("failed") == "fail"
    assert job_tone("embedding") == "running"
    assert job_tone("unknown-state") == ""


def test_formatting_helpers():
    assert short_id("abcdef123456") == "abcdef12"
    assert short_id("") == "—"
    assert ms(21.234) == "21ms"
    assert ms(0.25) == "0.25ms"
    assert ms(None) == "—"
    assert score(0.03125) == "0.0312"
    assert score(None) == "—"
    assert filesize(None) == "—"
    assert filesize(512) == "512B"
    assert filesize(2 * 1024 * 1024) == "2.0MB"


def test_timeago_degrades_gracefully_on_bad_input():
    assert timeago(None) == "—"
    assert timeago("not-a-date") == "not-a-date"


def owners_of(segments, fragment):
    return next(own for frag, own in segments if frag == fragment)


def test_coverage_marks_a_single_span():
    segments = coverage_segments("夫天地者万物之逆旅也", [(0, 3, 1)])
    assert segments == [("夫天地", frozenset({1})), ("者万物之逆旅也", frozenset())]


def test_coverage_keeps_overlap_instead_of_dropping_it():
    """highlight_segments drops the second span here; this must not."""
    segments = coverage_segments("零一二三四五", [(0, 4, 1), (2, 6, 2)])
    assert segments == [
        ("零一", frozenset({1})),
        ("二三", frozenset({1, 2})),
        ("四五", frozenset({2})),
    ]


def test_coverage_merges_two_sources_covering_the_same_span():
    segments = coverage_segments("零一二三", [(1, 3, 1), (1, 3, 2)])
    assert owners_of(segments, "一二") == frozenset({1, 2})


def test_coverage_handles_a_contained_span():
    segments = coverage_segments("零一二三四五", [(0, 6, 1), (2, 4, 2)])
    assert segments == [
        ("零一", frozenset({1})),
        ("二三", frozenset({1, 2})),
        ("四五", frozenset({1})),
    ]


def test_coverage_joins_runs_with_identical_owners():
    """Adjacent spans from the same source emit one run, not two."""
    segments = coverage_segments("零一二三", [(0, 2, 1), (2, 4, 1)])
    assert segments == [("零一二三", frozenset({1}))]


def test_coverage_leaves_a_gap_between_non_adjacent_spans():
    segments = coverage_segments("零一二三四五", [(0, 2, 1), (4, 6, 2)])
    assert segments == [
        ("零一", frozenset({1})),
        ("二三", frozenset()),
        ("四五", frozenset({2})),
    ]


def test_coverage_drops_out_of_range_spans_rather_than_trusting_them():
    assert coverage_segments("零一二", [(0, 99, 1)]) == [("零一二", frozenset())]
    assert coverage_segments("零一二", [(-1, 2, 1)]) == [("零一二", frozenset())]
    assert coverage_segments("零一二", [(2, 2, 1)]) == [("零一二", frozenset())]


def test_coverage_handles_empty_inputs():
    assert coverage_segments("", [(0, 1, 1)]) == []
    assert coverage_segments("零一二", []) == [("零一二", frozenset())]

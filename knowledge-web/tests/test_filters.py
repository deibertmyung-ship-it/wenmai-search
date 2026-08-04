"""Filters carry provenance guarantees, so they get direct tests."""

from __future__ import annotations

from kbweb.filters import (
    breadcrumb,
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

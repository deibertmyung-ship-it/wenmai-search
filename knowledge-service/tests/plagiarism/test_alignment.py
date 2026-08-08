"""Migration baseline for the seed-and-extend alignment port.

Vectors come from noplag-engine's `tests/test_alignment.py` at commit
005da60faad21bf52702997d73583b78d8905d22 (Apache-2.0). This is the highest-risk
file in the port - it decides both whether a match is found and what character
range gets reported - so these pin algorithm output deliberately.
"""

from __future__ import annotations

import random
import string

from kbsvc.plagiarism.alignment.seed_extend import align

# Upstream carried these as parameter defaults; the port moved them to settings.
MIN_SEED = 30
MIN_PASSAGE = 50
TOLERANCE = 0.85


def run(query_id: str, query: str, candidates: list[tuple[str, str]], **kw):
    return align(
        query_id,
        query,
        candidates,
        min_seed_len=kw.pop("min_seed_len", MIN_SEED),
        min_passage_len=kw.pop("min_passage_len", MIN_PASSAGE),
        extend_tolerance=kw.pop("extend_tolerance", TOLERANCE),
        **kw,
    )


def _random_text(length: int, seed: int) -> str:
    rng = random.Random(seed)
    alphabet = string.ascii_lowercase + " "
    return "".join(rng.choice(alphabet) for _ in range(length))


# --- degenerate inputs --------------------------------------------------


def test_empty_query_returns_empty():
    assert run("q", "", [("c", "anything goes here")]) == []


def test_empty_candidates_returns_empty():
    assert run("q", "the quick brown fox jumps over the lazy dog", []) == []


def test_query_shorter_than_min_seed_returns_empty():
    assert run("q", "x" * 29, [("c", "x" * 100)]) == []


def test_candidate_shorter_than_min_seed_is_skipped():
    assert run("q", "x" * 100, [("c", "x" * 29)]) == []


# --- exact reuse --------------------------------------------------------


def test_identical_query_and_candidate_span_everything_at_score_one():
    text = "the quick brown fox jumps over the lazy dog. " * 4
    result = run("q1", text, [("c1", text)])
    assert len(result) == 1
    p = result[0]
    assert (p.query_chunk_id, p.candidate_chunk_id) == ("q1", "c1")
    assert (p.query_start, p.query_end) == (0, len(text))
    assert (p.candidate_start, p.candidate_end) == (0, len(text))
    assert p.score == 1.0
    assert p.match_type == "verbatim"


def test_exact_embedded_passage_is_fully_covered():
    """The reported range must contain the whole reused region - a passage that
    covers only part of it understates the finding."""
    plagiarized = (
        "the rain in spain falls mainly on the plain, but it never rains "
        "after lunchtime on the weekends and never on a tuesday morning."
    )
    query = "filler1 starting prefix " + plagiarized + " filler2 trailing suffix"
    candidate = "other prefix here xx " + plagiarized + " yy other suffix here"
    q_offset = query.index(plagiarized)
    c_offset = candidate.index(plagiarized)

    result = run("q", query, [("c", candidate)])
    assert len(result) == 1
    p = result[0]
    # Extension may stretch into adjacent filler when the local match ratio
    # happens to clear tolerance, so these are containment assertions.
    assert p.query_start <= q_offset
    assert p.query_end >= q_offset + len(plagiarized)
    assert p.candidate_start <= c_offset
    assert p.candidate_end >= c_offset + len(plagiarized)
    assert p.score >= 0.95


def test_unrelated_random_text_produces_nothing():
    query = _random_text(2000, seed=1)
    candidate = _random_text(2000, seed=2)
    assert run("q", query, [("c", candidate)]) == []


# --- multi-source -------------------------------------------------------


def test_multiple_sources_each_reported_separately():
    """A compiled document reusing two different works must attribute both."""
    a = "the rain in spain falls mainly on the plain and never on a tuesday. " * 2
    b = "all happy families are alike each unhappy family is unhappy in its own way. " * 2
    query = a + " joining text that belongs to neither source at all. " + b
    result = run("q", query, [("src-a", a), ("src-b", b)])
    assert {p.candidate_chunk_id for p in result} == {"src-a", "src-b"}


def test_output_is_deterministically_ordered():
    a = "the rain in spain falls mainly on the plain and never on a tuesday. " * 2
    b = "all happy families are alike each unhappy family is unhappy in its own way. " * 2
    query = a + " joining text belonging to neither one of the two sources. " + b
    candidates = [("src-b", b), ("src-a", a)]
    first = run("q", query, candidates)
    second = run("q", query, list(reversed(candidates)))
    keys = [(p.query_chunk_id, p.query_start, p.candidate_chunk_id) for p in first]
    assert keys == sorted(keys)
    assert keys == [(p.query_chunk_id, p.query_start, p.candidate_chunk_id) for p in second]


# --- kbsvc additions ----------------------------------------------------


def test_should_stop_abandons_remaining_candidates():
    """Not an upstream vector. Cancellation and the time budget both land here:
    the caller sets the flag, so partial output is expected, not a failure."""
    text = "the rain in spain falls mainly on the plain and never on a tuesday. " * 2
    candidates = [(f"c{i}", text) for i in range(5)]

    calls = {"n": 0}

    def stop_after_two() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    partial = run("q", text, candidates, should_stop=stop_after_two)
    full = run("q", text, candidates)
    assert 0 < len(partial) < len(full)


def test_should_stop_never_called_is_equivalent_to_omitting_it():
    text = "the rain in spain falls mainly on the plain and never on a tuesday. " * 2
    candidates = [("c1", text), ("c2", text)]
    assert run("q", text, candidates, should_stop=lambda: False) == run("q", text, candidates)


def test_chinese_verbatim_reuse_is_found_with_correct_offsets():
    """Not an upstream vector - upstream's baseline is entirely Latin. Offsets
    are what a report highlights, so they are asserted against the source."""
    reused = (
        "贼克者，取用之首法也。上克下为贼，下贼上为克。"
        "凡四课之中，有上克下者为贼，下贼上者为克。取用之道，先取贼克，次取比用。"
    )
    query = "前言部分，与来源无关的文字。" + reused + "后记部分，同样无关。"
    candidate = "另一部书的开头。" + reused + "另一部书的结尾。"

    result = run("q", query, [("c", candidate)], min_seed_len=12, min_passage_len=20)
    assert len(result) == 1
    p = result[0]
    q_offset = query.index(reused)
    assert p.query_start <= q_offset
    assert p.query_end >= q_offset + len(reused)
    assert query[p.query_start : p.query_end].find(reused) >= 0


def test_twenty_four_character_chinese_reuse_is_not_filtered_by_generic_defaults():
    """A production check must not apply the English 30/50 gates to Chinese."""
    text = "人禀天地、命属阴阳、生居覆载之内、尽在五行之中。"

    result = run("q-short", text, [("source", text)])

    assert len(result) == 1
    passage = result[0]
    assert (passage.query_start, passage.query_end) == (0, len(text))
    assert (passage.candidate_start, passage.candidate_end) == (0, len(text))
    assert passage.score == 1.0


def test_short_chinese_reuse_survives_punctuation_difference():
    query = "人禀天地、命属阴阳、生居覆载之内、尽在五行之中。"
    candidate = "人禀天地，命属阴阳，生居覆载之内，尽在五行之中。"

    result = run("q-punctuation", query, [("source", candidate)], min_seed_len=12,
                 min_passage_len=20)

    assert len(result) == 1
    assert result[0].score >= 0.99

"""Migration baseline for the winnowing port.

These vectors come from noplag-engine's `tests/test_winnowing.py` at commit
005da60faad21bf52702997d73583b78d8905d22 (Apache-2.0). Their job is to prove the
port did not change algorithm semantics - so unlike the rest of this suite, they
deliberately pin implementation output rather than user-visible behaviour.

If one of these fails after a refactor, the refactor changed the algorithm.
"""

from __future__ import annotations

from kbsvc.plagiarism.fingerprinting import fingerprint, normalize

# Upstream carried these as parameter defaults; the port moved them to settings,
# so the baseline states them explicitly.
K = 5
W = 8

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def fp(text: str, k: int = K, w: int = W) -> list[int]:
    return fingerprint(text, k=k, w=w)


# --- degenerate inputs --------------------------------------------------


def test_empty_string_returns_empty():
    assert fp("") == []


def test_whitespace_only_returns_empty():
    assert fp("    \n\t  ") == []


def test_text_shorter_than_k_returns_empty():
    assert fp("abc") == []
    assert fp("abcd") == []


def test_text_exactly_k_yields_one_fingerprint():
    assert len(fp("hello")) == 1


def test_text_between_k_and_w_uses_degenerate_window():
    # 10 chars at k=5 -> 6 k-grams; 6 <= w=8 -> single degenerate window.
    assert len(fp("a" * 10)) == 1


# --- determinism --------------------------------------------------------


def test_determinism():
    text = "The quick brown fox jumps over the lazy dog."
    runs = [fp(text) for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_output_fits_in_signed_int64_range():
    # PostgreSQL BIGINT[] is the storage type; anything outside this range
    # would fail on insert rather than at fingerprint time.
    for h in fp("the quick brown fox jumps over the lazy dog. " * 50):
        assert INT64_MIN <= h <= INT64_MAX


# --- normalization ------------------------------------------------------


def test_normalization_makes_input_case_and_whitespace_insensitive():
    a = fp("The Quick Brown Fox Jumps Over The Lazy Dog")
    b = fp("the   quick\tbrown\nfox jumps over the lazy dog")
    assert a == b


def test_citation_markers_make_pasted_text_match_marker_free_corpus():
    pasted = "The fact was true.[12] However it changed.[citation needed] Later[3] it held."
    corpus = "The fact was true. However it changed. Later it held."
    assert normalize(pasted) == normalize(corpus)
    assert fp(pasted) == fp(corpus)


def test_normalize_keeps_legitimate_bracketed_content():
    assert "[the appendix]" in normalize("See [the appendix] for the full proof")
    assert "[redacted]" in normalize("the value was [redacted] in the report")


# --- matching guarantees ------------------------------------------------


def test_substantial_overlap_produces_intersection():
    common = "the quick brown fox jumps over the lazy dog " * 5
    doc1 = "unique prefix of doc one " + common + " differing suffix one"
    doc2 = "another unique prefix doc " + common + " differing suffix two"
    assert len(set(fp(doc1)) & set(fp(doc2))) >= 5


def test_unrelated_docs_have_minimal_intersection():
    doc1 = "the quick brown fox jumps over the lazy dog. " * 3
    doc2 = "lorem ipsum dolor sit amet consectetur adipiscing elit. " * 3
    assert len(set(fp(doc1)) & set(fp(doc2))) < 5


def test_long_common_substring_produces_shared_fingerprint():
    # Schleimer's guarantee: any common substring of length >= w + k - 1 (12
    # here) yields at least one shared fingerprint.
    common = "the quick brown fox jumps"
    doc1 = "x" * 50 + " " + common + " " + "y" * 50
    doc2 = "p" * 50 + " " + common + " " + "q" * 50
    assert len(set(fp(doc1)) & set(fp(doc2))) >= 1


# --- parameter sensitivity ----------------------------------------------


def test_different_k_changes_output():
    text = "the quick brown fox jumps over the lazy dog"
    assert fp(text, k=5) != fp(text, k=6)


def test_wider_window_selects_no_more_fingerprints():
    text = "the quick brown fox jumps over the lazy dog. " * 10
    short_window = fp(text, w=4)
    long_window = fp(text, w=12)
    assert short_window != long_window
    assert len(long_window) <= len(short_window)


# --- classical Chinese --------------------------------------------------


def test_chinese_text_fingerprints_and_matches_itself():
    """Not an upstream vector. This corpus is classical Chinese, and the
    upstream baseline is entirely Latin - a port that silently broke on CJK
    would pass every test above."""
    text = "贼克者，取用之首法也。上克下为贼，下贼上为克。" * 3
    prints = fp(text)
    assert prints
    assert prints == fp(text)
    quoted = "前言。" + text + "。后语"
    assert len(set(prints) & set(fp(quoted))) >= 1

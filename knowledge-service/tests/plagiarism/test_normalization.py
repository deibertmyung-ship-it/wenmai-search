from __future__ import annotations

from kbsvc.plagiarism.normalization import NORMALIZER_VERSION, normalize_with_offsets


def test_chinese_normalization_ignores_punctuation_and_whitespace_but_keeps_offsets():
    text = (
        "\u4eba\u7980\u5929\u5730\u3001\u547d\u5c5e\u9634\u9633\uff0c\n"
        "\u751f\u5c45\u8986\u8f7d\u4e4b\u5185\u3002"
    )
    normalized = normalize_with_offsets(text, profile="zh")

    assert normalized.text == (
        "\u4eba\u7980\u5929\u5730\u547d\u5c5e\u9634\u9633\u751f\u5c45\u8986\u8f7d\u4e4b\u5185"
    )
    assert normalized.effective_chars == 14
    assert normalized.original_span(0, len(normalized.text)) == (0, len(text) - 1)
    assert normalized.original_span_with_boundaries(0, len(normalized.text), text) == (0, len(text))


def test_citation_markers_are_removed_consistently():
    text = "\u4e8b\u5b9e[12]\u3002\u540e\u6765\u53d8\u4e86[citation needed]\u3002"
    normalized = normalize_with_offsets(text, profile="zh")
    assert "12" not in normalized.text
    assert normalized.text == "\u4e8b\u5b9e\u540e\u6765\u53d8\u4e86"


def test_generic_profile_preserves_punctuation_and_whitespace_normalizes():
    normalized = normalize_with_offsets("The  quick\nfox.", profile="generic")
    assert normalized.text == "the quick fox."
    assert normalized.effective_chars == len("thequickfox")


def test_pure_punctuation_has_no_effective_characters():
    assert normalize_with_offsets("\u3001\u3002!? \n", profile="zh").effective_chars == 0


def test_normalizer_version_is_explicit():
    assert NORMALIZER_VERSION == "plag-normalizer-v2"


def test_offsets_cover_empty_spans_and_nfkc_expansion():
    normalized = normalize_with_offsets("A\uff21B", profile="generic")
    assert normalized.text == "aab"
    assert normalized.original_span(0, 0) == (0, 0)
    assert normalized.original_span(1, 2) == (1, 2)
    assert normalized.original_span(3, 3) == (3, 3)


def test_original_slice_contains_punctuation_around_a_match():
    text = "前文——人禀天地、命属阴阳。后文"
    normalized = normalize_with_offsets(text, profile="zh")
    start = normalized.text.index("人禀天地")
    end = start + len("人禀天地命属阴阳")
    original_start, original_end = normalized.original_span_with_boundaries(start, end, text)
    assert "人禀天地、命属阴阳" in text[original_start:original_end]

from __future__ import annotations

from kbsvc.plagiarism.fingerprinting import fingerprint
from kbsvc.plagiarism.matching_policy import resolve_match_policy
from kbsvc.plagiarism.projection import _chunk_fingerprints


def test_chinese_policy_uses_zh_thresholds(settings):
    policy = resolve_match_policy(
        "\u4eba\u7980\u5929\u5730\u3001\u547d\u5c5e\u9634\u9633\u3002", "auto", settings
    )
    assert policy.profile == "zh"
    assert policy.min_seed_len == settings.plag_zh_min_seed_len == 12
    assert policy.min_passage_len == settings.plag_zh_min_passage_len == 20
    assert policy.matcher_version == "seed-extend-v2"


def test_english_policy_keeps_generic_thresholds(settings):
    policy = resolve_match_policy("The quick brown fox jumps over the lazy dog.", "en", settings)
    assert policy.profile == "generic"
    assert policy.min_seed_len == settings.plag_min_seed_len == 30
    assert policy.min_passage_len == settings.plag_min_passage_len == 50


def test_explicit_english_wins_for_mixed_text(settings):
    policy = resolve_match_policy("\u4eba\u7980\u5929\u5730 The quick brown fox", "en", settings)
    assert policy.profile == "generic"
    assert policy.min_seed_len == 30


def test_mixed_script_projection_keeps_chinese_and_generic_recall(settings):
    source = (
        "English context. "
        "\u4eba\u7980\u5929\u5730\u3001\u547d\u5c5e\u9634\u9633\u3002 More context."
    )
    stored = set(_chunk_fingerprints(source, settings))
    zh = set(
        fingerprint(source, settings.plag_kgram, settings.plag_winnow_window, profile="zh")
    )
    generic = set(
        fingerprint(source, settings.plag_kgram, settings.plag_winnow_window, profile="generic")
    )
    assert zh <= stored
    assert generic <= stored


def test_short_chinese_policy_enables_only_exact_channel(settings):
    policy = resolve_match_policy(
        "\u4eba\u7980\u5929\u5730\u3001\u547d\u5c5e\u9634\u9633\u751f\u5c45\u8986\u8f7d\u3002",
        "zh",
        settings,
    )
    assert policy.short_exact_enabled
    assert policy.short_exact_min_score == settings.plag_short_exact_min_score

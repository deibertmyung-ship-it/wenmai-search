"""Language-specific matching policy for one plagiarism check."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from ..config import Settings
from .language import detect_language, is_chinese_dominant
from .normalization import NORMALIZER_VERSION, normalize_with_offsets

MATCHER_VERSION = "seed-extend-v2"


@dataclass(frozen=True)
class MatchPolicy:
    matcher_version: str
    resolved_language: str
    profile: Literal["zh", "generic"]
    effective_chars: int
    min_effective_chars: int
    min_seed_len: int
    min_passage_len: int
    short_exact_enabled: bool
    short_exact_min_score: float
    normalizer_version: str

    def to_config(self) -> dict:
        return asdict(self)

    @classmethod
    def from_config(cls, config: dict) -> MatchPolicy:
        """Restore a frozen policy written by an earlier run."""
        return cls(
            matcher_version=str(config["matcher_version"]),
            resolved_language=str(config["resolved_language"]),
            profile="zh" if config["profile"] == "zh" else "generic",
            effective_chars=int(config["effective_chars"]),
            min_effective_chars=int(config["min_effective_chars"]),
            min_seed_len=int(config["min_seed_len"]),
            min_passage_len=int(config["min_passage_len"]),
            short_exact_enabled=bool(config["short_exact_enabled"]),
            short_exact_min_score=float(config["short_exact_min_score"]),
            normalizer_version=str(config["normalizer_version"]),
        )


def resolve_match_policy(text: str, declared_language: str, settings: Settings) -> MatchPolicy:
    detected = declared_language if declared_language != "auto" else detect_language(text)
    base_language = detected.split("-", 1)[0]
    # An explicit language is authoritative.  Only auto mode uses the Han
    # ratio heuristic, because short classical Chinese is easy for generic
    # language detectors to misclassify.
    profile: Literal["zh", "generic"] = (
        "zh"
        if base_language == "zh" or (declared_language == "auto" and is_chinese_dominant(text))
        else "generic"
    )
    normalized = normalize_with_offsets(text, profile=profile)
    if profile == "zh":
        min_seed_len = settings.plag_zh_min_seed_len
        min_passage_len = settings.plag_zh_min_passage_len
    else:
        min_seed_len = settings.plag_min_seed_len
        min_passage_len = settings.plag_min_passage_len
    return MatchPolicy(
        matcher_version=MATCHER_VERSION,
        resolved_language="zh" if profile == "zh" and base_language not in {"zh"} else detected,
        profile=profile,
        effective_chars=normalized.effective_chars,
        min_effective_chars=settings.plag_min_effective_chars,
        min_seed_len=min_seed_len,
        min_passage_len=min_passage_len,
        short_exact_enabled=profile == "zh"
        and settings.plag_min_effective_chars <= normalized.effective_chars < min_passage_len,
        short_exact_min_score=settings.plag_short_exact_min_score,
        normalizer_version=NORMALIZER_VERSION,
    )

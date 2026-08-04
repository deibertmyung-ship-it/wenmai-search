"""Query rewriting.

Deliberately rule-based: an LLM call in the hot path costs latency and money,
and for a classical-Chinese corpus the useful transforms are mechanical
(strip interrogatives, drop framing verbs, split multi-intent questions).
Set KB_REWRITE off per request when you want the raw query.
"""

from __future__ import annotations

import re

_QUESTION_TAIL = re.compile(r"[?？。！!]+$")
_FRAMING = (
    "请问", "请", "帮我", "我想知道", "告诉我", "解释一下", "介绍一下",
    "什么是", "如何", "怎样", "怎么", "为什么", "是什么",
    "what is", "how to", "how do i", "tell me about", "explain",
)
_SPLIT = re.compile(r"[,，;；]|(?:\s+and\s+)|(?:\s*以及\s*)|(?:\s*并且\s*)")
_MIN_VARIANT_CHARS = 2


def rewrite(query: str, *, enabled: bool = True, max_variants: int = 3) -> list[str]:
    """Return the original query first, then de-noised variants."""
    original = query.strip()
    if not original:
        return []
    if not enabled:
        return [original]

    variants: list[str] = [original]
    stripped = _QUESTION_TAIL.sub("", original).strip()
    core = stripped
    lowered = core.lower()
    for phrase in _FRAMING:
        if lowered.startswith(phrase):
            core = core[len(phrase) :].strip("的 　")
            lowered = core.lower()
    if core and core != original and len(core) >= _MIN_VARIANT_CHARS:
        variants.append(core)

    for part in _SPLIT.split(core):
        candidate = (part or "").strip()
        if len(candidate) >= _MIN_VARIANT_CHARS and candidate not in variants:
            variants.append(candidate)

    seen: set[str] = set()
    unique = [v for v in variants if not (v in seen or seen.add(v))]
    return unique[:max_variants]

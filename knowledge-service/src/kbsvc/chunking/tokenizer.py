"""Cheap, dependency-free token estimation and sentence splitting.

CJK is ~1 token per character for most tokenizers; Latin script is ~4 chars per
token. Good enough to size chunks without pulling in a tokenizer model.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"(?<=[。！？；!?;\n])")
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 4) or 1


def chars_for_tokens(tokens: int, text_sample: str) -> int:
    """Invert estimate_tokens for a given script mix."""
    if not text_sample:
        return tokens
    ratio = estimate_tokens(text_sample) / len(text_sample)
    return max(int(tokens / ratio), 1) if ratio else tokens


def split_sentences(text: str) -> list[str]:
    """Split on CJK and Latin sentence terminators, keeping the terminator."""
    parts = [part for part in _SENTENCE_END.split(text) if part]
    if not parts:
        return [text] if text else []
    return parts

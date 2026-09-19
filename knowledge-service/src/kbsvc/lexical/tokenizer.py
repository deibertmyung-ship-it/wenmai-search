"""Character n-gram tokenizer for the corpus.

Why not a word tokenizer: this corpus is classical Chinese, where modern word
segmenters mis-split constantly. Character unigrams + bigrams need no model, no
download, and recall 文言文 terms reliably. Latin runs stay whole and lowercased,
because splitting `hybrid` into characters helps nobody.

This is the one piece of the old hand-rolled BM25 worth keeping: the scoring
belongs to a real inverted index, but the analysis is domain knowledge.
Tantivy sees the output of this function as whitespace-separated tokens, so its
own analyzer only has to split on spaces.
"""

from __future__ import annotations

import re

from ..normalize import normalize

_TOKEN_SPLIT = re.compile(r"[^\w㐀-䶿一-鿿豈-﫿]+", re.UNICODE)
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def tokenize(text: str) -> list[str]:
    """CJK -> unigrams + bigrams; Latin/digits -> lowercased words.

    Traditional and old glyph forms are folded first, so indexing and querying
    agree without either side having to remember to do it - this function is the
    single entry point for both.
    """
    tokens: list[str] = []
    for segment in _TOKEN_SPLIT.split(normalize(text).lower()):
        if not segment:
            continue
        if _CJK_CHAR.search(segment):
            chars = [ch for ch in segment if not ch.isspace()]
            tokens.extend(chars)
            tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
        else:
            tokens.append(segment)
    return tokens


def analyze(text: str) -> str:
    """Tokenize into the whitespace-joined form the lexical index stores."""
    return " ".join(tokenize(text))

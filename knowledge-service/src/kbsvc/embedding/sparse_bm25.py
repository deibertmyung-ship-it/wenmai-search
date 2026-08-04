"""Character n-gram BM25 sparse encoder.

Why not a word tokenizer: this corpus is classical Chinese, where modern word
segmenters mis-split constantly. Character unigrams + bigrams need no model, no
download, and recall 文言文 terms reliably.

BM25 is split the standard way: the document vector carries length-normalized
term frequency, the query vector carries IDF. Their dot product is the BM25
score, so Qdrant can compute it natively.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter
from typing import Protocol

from .base import SparseVector

K1 = 1.5
B = 0.75
_TOKEN_SPLIT = re.compile(r"[^\w㐀-䶿一-鿿豈-﫿]+", re.UNICODE)
_CJK_CHAR = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_INDEX_SPACE = 2**31


class TermStatsProvider(Protocol):
    """Supplies corpus-level statistics; backed by the metadata store."""

    def doc_freq(self, terms: list[str]) -> dict[str, int]: ...

    def chunk_count(self) -> int: ...

    def avg_length(self) -> float: ...


class StaticTermStats:
    """In-memory stats, used for tests and for cold-start query encoding."""

    def __init__(
        self, freqs: dict[str, int] | None = None, count: int = 0, avg_len: float = 1.0
    ) -> None:
        self._freqs = freqs or {}
        self._count = count
        self._avg = avg_len or 1.0

    def doc_freq(self, terms: list[str]) -> dict[str, int]:
        return {term: self._freqs.get(term, 0) for term in terms}

    def chunk_count(self) -> int:
        return self._count

    def avg_length(self) -> float:
        return self._avg


def tokenize(text: str) -> list[str]:
    """CJK -> unigrams + bigrams; Latin/digits -> lowercased words."""
    tokens: list[str] = []
    for segment in _TOKEN_SPLIT.split(text.lower()):
        if not segment:
            continue
        if _CJK_CHAR.search(segment):
            chars = [ch for ch in segment if not ch.isspace()]
            tokens.extend(chars)
            tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
        else:
            tokens.append(segment)
    return tokens


def term_index(term: str) -> int:
    """Stable 31-bit index. Qdrant sparse vectors are keyed by uint32."""
    return zlib.crc32(term.encode("utf-8")) % _INDEX_SPACE


def idf(doc_freq: int, chunk_count: int) -> float:
    """Robertson/Sparck-Jones IDF, floored so common terms never go negative."""
    numerator = chunk_count - doc_freq + 0.5
    denominator = doc_freq + 0.5
    from math import log

    return max(log(1 + numerator / denominator), 0.01)


class Bm25SparseEmbedder:
    name = "bm25-chargram"

    def __init__(self, stats: TermStatsProvider) -> None:
        self.stats = stats

    def term_frequencies(self, text: str) -> Counter[str]:
        return Counter(tokenize(text))

    def encode_document(self, text: str) -> SparseVector:
        counts = self.term_frequencies(text)
        length = sum(counts.values()) or 1
        avg = self.stats.avg_length() or 1.0
        norm = K1 * (1 - B + B * (length / avg))
        vector: SparseVector = {}
        for term, tf in counts.items():
            vector[term_index(term)] = (tf * (K1 + 1)) / (tf + norm)
        return vector

    def encode_query(self, text: str) -> SparseVector:
        counts = self.term_frequencies(text)
        if not counts:
            return {}
        terms = list(counts)
        freqs = self.stats.doc_freq(terms)
        total = self.stats.chunk_count()
        vector: SparseVector = {}
        for term in terms:
            weight = idf(freqs.get(term, 0), total) if total else 1.0
            vector[term_index(term)] = weight
        return vector

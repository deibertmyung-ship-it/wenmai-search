"""Winnowing fingerprints for L1 retrieval.

Implements robust winnowing from Schleimer, Wilkerson & Aiken (SIGMOD 2003) -
"Winnowing: Local Algorithms for Document Fingerprinting". Each document maps
to a deduplicated set of selected k-gram hashes; the L1 retrieval layer queries
PostgreSQL GIN with the array-overlap operator `&&` over these fingerprints.

Algorithm: normalize -> extract sliding-character k-grams -> hash each to a
signed 64-bit int (PostgreSQL bigint range) -> slide a window of w consecutive
hashes -> select the rightmost minimum from each window -> emit only when the
selected position differs from the previous window's selection (robust
winnowing - guarantees that any common substring of length >= w + k - 1
produces at least one shared fingerprint).

Ported from noplag-engine `src/noplag_engine/fingerprinting/winnowing.py` at
commit 005da60faad21bf52702997d73583b78d8905d22, Apache-2.0 (see
`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`).
Modified for kbsvc: k/w defaults are supplied by the caller from settings
rather than being baked in as parameter defaults, so the algorithm config hash
has a single source. Hashing, normalization and selection are unchanged - this
file is covered by a migration-baseline test against upstream vectors.
"""

from __future__ import annotations

import hashlib

from ..normalization import normalize_with_offsets


def fingerprint(text: str, k: int, w: int, *, profile: str = "generic") -> list[int]:
    """Compute the winnowing fingerprint of `text`.

    Returns a sorted, deduplicated list of signed 64-bit ints, each derived
    from a k-gram of the normalized input. Empty when the normalized text has
    fewer than `k` characters.

    Output is deterministic across runs and machines (blake2b is keyless and
    reproducible - unlike Python's built-in `hash()`, which is salted).
    """
    normalized = normalize_with_offsets(text, profile=profile).text
    if len(normalized) < k:
        return []

    hashes = [_hash_kgram(normalized[i : i + k]) for i in range(len(normalized) - k + 1)]
    return sorted(_winnow(hashes, w))


def normalize(text: str) -> str:
    """NFKC + de-citation + lowercase + whitespace collapse.

    Punctuation is kept - stylistic fingerprints often live there, and the
    alignment stage handles paraphrase-level differences.

    Exposed (upstream kept it private) because the corpus projection stores the
    normalized form's length alongside the fingerprints, and computing it twice
    from different code paths is how the two silently diverge.
    """
    return normalize_with_offsets(text, profile="generic").text


def _hash_kgram(s: str) -> int:
    # blake2b with digest_size=8 returns 8 bytes (64 bits); reading as
    # big-endian signed maps the value into PostgreSQL signed bigint range
    # (-2^63 .. 2^63 - 1). Cryptographic strength is overkill but it gives
    # uniform distribution and zero implementation risk.
    digest = hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _winnow(hashes: list[int], w: int) -> set[int]:
    if not hashes:
        return set()
    # Degenerate case: fewer hashes than the window. Schleimer's matching
    # guarantee no longer applies, but the document still needs a fingerprint
    # for L1 retrieval - emit the minimum of what we have.
    if len(hashes) <= w:
        return {min(hashes)}

    selected_positions: set[int] = set()
    last_emitted = -1  # sentinel - no position can equal -1, so first window emits
    for window_start in range(len(hashes) - w + 1):
        # Rightmost minimum: scan window left-to-right, replace on <= (not <).
        # When two positions tie, the later one wins.
        min_pos = window_start
        for j in range(window_start + 1, window_start + w):
            if hashes[j] <= hashes[min_pos]:
                min_pos = j
        if min_pos != last_emitted:
            selected_positions.add(min_pos)
            last_emitted = min_pos

    return {hashes[i] for i in selected_positions}

"""Text alignment - seed-and-extend over query/candidate chunk pairs.

Final precision stage. Candidate retrieval produces chunk pairs; this turns each
(query, candidate) pair into the character-offset-bearing passages a report
renders.

BLAST-adapted for text
----------------------
The shape is borrowed from Altschul et al. (1990, BLAST) but adapted for natural
language rather than biological sequences:

1. **Seed** - find every maximal exact common substring of length
   >= `min_seed_len`. Maximal means it cannot be extended in either direction
   without introducing a mismatch. Implemented by indexing the candidate's
   `min_seed_len` windows in a dict; verification is exact string equality, so
   hash collisions cost CPU but do not affect output.

2. **Extend** - from each seed's boundaries, walk outward in fixed lookahead
   windows. A boundary advances when the per-window match ratio meets
   `extend_tolerance`. Deliberately simpler than gap-tolerant Smith-Waterman:
   it handles single-character substitutions (the dominant edit in lightly
   paraphrased reuse) but not insertions or deletions.

3. **Merge + filter** - seeds on the same diagonal (`c_start - q_start`) often
   extend across each other; merged passages replace the overlapping pieces.
   Passages shorter than `min_passage_len` are then dropped.

Score per passage is `matching_chars / max(q_len, c_len)` over the aligned
region. The extend stage advances both sides in lock-step, so the two lengths
are equal today; the `max()` form is kept for forward compatibility with a
gap-tolerant extender.

Ported from noplag-engine `src/noplag_engine/alignment/seed_extend.py` at commit
005da60faad21bf52702997d73583b78d8905d22, Apache-2.0 (see
`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`).
Modified for kbsvc: chunk identifiers are `str` rather than `UUID` (kbsvc ids
are strings throughout); tuning parameters are supplied by the caller from
settings instead of defaulting here; and `align` accepts a `should_stop`
callback checked between candidates so a cancelled or budget-exhausted check
stops promptly instead of running the pair list to the end. The seeding,
extension, merge, dedupe and scoring logic is unchanged and is covered by a
migration-baseline test against upstream vectors.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from ..language import is_chinese_dominant
from ..normalization import NormalizedText, normalize_with_offsets

# Lookahead width for the tolerant-extend stage (chars). Small enough to react
# quickly when the text actually diverges, large enough that a couple of
# substituted chars don't kill the window.
_EXTEND_LOOKAHEAD = 20

# When merging passages along the same diagonal, treat query ranges within this
# many chars of each other as adjoining. The tolerant-extend stage walks across
# short mismatch runs, so adjacent same-diagonal seeds usually already overlap
# by the time merge runs - this gap covers the residual case of a short mismatch
# run that stopped both extensions just shy of meeting.
_MERGE_GAP = 20

# When one pair yields several maximal exact matches whose query ranges overlap
# - typical when either side contains self-repeating text - the report wants one
# attribution per query region, not the full cross product. After merge, keep
# the longest and drop any later passage overlapping a kept one by more than
# this fraction.
_QUERY_OVERLAP_DEDUPE_THRESHOLD = 0.5


@dataclass(frozen=True)
class AlignedPassage:
    """A single aligned passage between a query chunk and a candidate chunk.

    `query_start` / `query_end` index into the **query chunk text** passed to
    `align()`. `candidate_start` / `candidate_end` index into the **candidate
    chunk text**, not the full source document. Callers that need document
    coordinates add the chunk's own `char_start` on each side - the check
    workflow does this before persisting, so stored findings are already in
    document coordinates.
    """

    query_chunk_id: str
    candidate_chunk_id: str
    query_start: int
    query_end: int
    candidate_start: int
    candidate_end: int
    score: float
    # Match kind, so consumers can distinguish alignment sources if softer
    # detection layers are ever added. Always "verbatim" here.
    match_type: str = "verbatim"


@dataclass(frozen=True)
class _Seed:
    q_start: int
    q_end: int
    c_start: int
    c_end: int


def align(
    query_chunk_id: str,
    query_text: str,
    candidates: list[tuple[str, str]],
    *,
    min_seed_len: int,
    min_passage_len: int,
    extend_tolerance: float,
    should_stop: Callable[[], bool] | None = None,
    normalizer_profile: str = "auto",
) -> list[AlignedPassage]:
    """Align `query_text` against each candidate; return passage spans.

    `candidates` is a list of `(chunk_id, text)`. Results are sorted by
    `(query_chunk_id, query_start, candidate_chunk_id)` for determinism.

    `should_stop`, when supplied, is polled before each candidate. Returning
    True abandons the remaining candidates and returns what was found so far -
    the caller owns the stop condition, so it already knows the result is
    partial and is responsible for labelling the report accordingly.

    Returns empty when the query is shorter than `min_seed_len`, there are no
    candidates, or nothing survives merge and filtering.
    """
    if not query_text or not candidates:
        return []

    if normalizer_profile == "auto":
        normalizer_profile = "zh" if is_chinese_dominant(query_text) else "generic"
    if normalizer_profile == "zh" and (min_seed_len, min_passage_len) == (30, 50):
        # Keep direct callers safe while the worker supplies an explicit
        # language policy. English callers retain the historical 30/50 gates.
        min_seed_len, min_passage_len = 12, 20

    normalized_query = normalize_with_offsets(query_text, profile=normalizer_profile)
    if normalized_query.effective_chars == 0:
        return []
    if len(normalized_query.text) < min_seed_len:
        return []

    passages: list[AlignedPassage] = []
    for candidate_chunk_id, candidate_text in candidates:
        if should_stop is not None and should_stop():
            break
        normalized_candidate = normalize_with_offsets(candidate_text, profile=normalizer_profile)
        if normalized_candidate.effective_chars == 0:
            continue
        if len(normalized_candidate.text) < min_seed_len:
            continue

        if (
            normalizer_profile == "zh"
            and min_seed_len <= normalized_query.effective_chars < min_passage_len
        ):
            passages.extend(
                _short_exact_matches(
                    query_chunk_id,
                    query_text,
                    normalized_query,
                    candidate_chunk_id,
                    candidate_text,
                    normalized_candidate,
                )
            )
            continue

        seeds = _find_seeds(normalized_query.text, normalized_candidate.text, min_seed_len)
        if not seeds:
            continue
        extended = [
            _extend(seed, normalized_query.text, normalized_candidate.text, extend_tolerance)
            for seed in seeds
        ]
        merged = _merge(extended)
        deduped = _dedupe_by_query_coverage(merged)
        for span in deduped:
            if span.q_end - span.q_start < min_passage_len:
                continue
            passages.append(
                _score_and_build(
                    span,
                    query_chunk_id,
                    candidate_chunk_id,
                    query_text,
                    candidate_text,
                    normalized_query,
                    normalized_candidate,
                )
            )

    passages.sort(key=lambda p: (p.query_chunk_id, p.query_start, p.candidate_chunk_id))
    return passages


def _short_exact_matches(
    query_chunk_id: str,
    query_text: str,
    normalized_query: NormalizedText,
    candidate_chunk_id: str,
    candidate_text: str,
    normalized_candidate: NormalizedText,
) -> list[AlignedPassage]:
    """Return only complete Chinese short-query occurrences."""
    needle = normalized_query.text
    if not needle:
        return []

    passages: list[AlignedPassage] = []
    start = 0
    while True:
        normalized_start = normalized_candidate.text.find(needle, start)
        if normalized_start < 0:
            break
        normalized_end = normalized_start + len(needle)
        # The query side always spans the full text: the needle is
        # ``normalized_query.text`` in its entirety, so there is nothing to
        # offset.  The candidate side needs boundary mapping because the
        # match may start at any position inside the candidate.
        c_start, c_end = normalized_candidate.original_span_with_boundaries(
            normalized_start, normalized_end, candidate_text
        )
        if normalized_start == 0 and normalized_end == len(normalized_candidate.text):
            c_start, c_end = 0, len(candidate_text)
        passages.append(
            AlignedPassage(
                query_chunk_id=query_chunk_id,
                candidate_chunk_id=candidate_chunk_id,
                query_start=0,
                query_end=len(query_text),
                candidate_start=c_start,
                candidate_end=c_end,
                score=1.0,
                match_type="short_exact",
            )
        )
        start = normalized_start + 1
    return passages


def _find_seeds(query: str, candidate: str, min_seed_len: int) -> list[_Seed]:
    """Return every maximal exact common substring of length >= min_seed_len.

    Hash-indexed seeding: every `min_seed_len`-window of the candidate goes into
    a dict, then query windows look it up. On a hit the match is extended
    bidirectionally to its maximal exact extent. Maximal matches are deduped on
    `(q_start, c_start)` so re-discovery from neighbouring windows is a set
    lookup rather than redundant extension work.
    """
    cand_index: dict[str, list[int]] = defaultdict(list)
    for i in range(len(candidate) - min_seed_len + 1):
        cand_index[candidate[i : i + min_seed_len]].append(i)

    seen: set[tuple[int, int]] = set()
    seeds: list[_Seed] = []

    qi = 0
    while qi <= len(query) - min_seed_len:
        window = query[qi : qi + min_seed_len]
        positions = cand_index.get(window)
        if not positions:
            qi += 1
            continue
        longest = 0
        for c_pos in positions:
            left = _extend_exact_left(query, candidate, qi, c_pos)
            right = _extend_exact_right(query, candidate, qi + min_seed_len, c_pos + min_seed_len)
            q_start = qi - left
            c_start = c_pos - left
            key = (q_start, c_start)
            if key in seen:
                longest = max(longest, min_seed_len + left + right)
                continue
            seen.add(key)
            seeds.append(
                _Seed(
                    q_start=q_start,
                    q_end=q_start + min_seed_len + left + right,
                    c_start=c_start,
                    c_end=c_start + min_seed_len + left + right,
                )
            )
            longest = max(longest, min_seed_len + left + right)
        # Skip past the just-discovered maximal match. Other maximal matches
        # starting inside this range cannot exist at the same diagonal - they
        # would be sub-matches of the one just found. Different-diagonal ones
        # are still found, because this loop iterates every c_pos for the
        # current window and has already emitted them.
        qi += max(1, longest - min_seed_len + 1)
    return seeds


def _extend_exact_left(query: str, candidate: str, qi: int, ci: int) -> int:
    """Maximal exact extension to the left of (qi, ci). Returns char count."""
    n = 0
    while qi - n - 1 >= 0 and ci - n - 1 >= 0 and query[qi - n - 1] == candidate[ci - n - 1]:
        n += 1
    return n


def _extend_exact_right(query: str, candidate: str, qi: int, ci: int) -> int:
    """Maximal exact extension to the right of (qi, ci). Returns char count."""
    n = 0
    while qi + n < len(query) and ci + n < len(candidate) and query[qi + n] == candidate[ci + n]:
        n += 1
    return n


def _extend(seed: _Seed, query: str, candidate: str, tolerance: float) -> _Seed:
    """Tolerant lookahead extension on both sides of a maximal exact seed.

    Boundaries advance by `_EXTEND_LOOKAHEAD` chars per step while the per-window
    match ratio meets `tolerance`. Both sides walk together, so
    `q_end - q_start` always equals `c_end - c_start`.
    """
    q_end, c_end = seed.q_end, seed.c_end
    while q_end + _EXTEND_LOOKAHEAD <= len(query) and c_end + _EXTEND_LOOKAHEAD <= len(candidate):
        q_window = query[q_end : q_end + _EXTEND_LOOKAHEAD]
        c_window = candidate[c_end : c_end + _EXTEND_LOOKAHEAD]
        if _match_ratio(q_window, c_window) >= tolerance:
            q_end += _EXTEND_LOOKAHEAD
            c_end += _EXTEND_LOOKAHEAD
        else:
            break

    q_start, c_start = seed.q_start, seed.c_start
    while q_start - _EXTEND_LOOKAHEAD >= 0 and c_start - _EXTEND_LOOKAHEAD >= 0:
        q_window = query[q_start - _EXTEND_LOOKAHEAD : q_start]
        c_window = candidate[c_start - _EXTEND_LOOKAHEAD : c_start]
        if _match_ratio(q_window, c_window) >= tolerance:
            q_start -= _EXTEND_LOOKAHEAD
            c_start -= _EXTEND_LOOKAHEAD
        else:
            break

    return _Seed(q_start=q_start, q_end=q_end, c_start=c_start, c_end=c_end)


def _match_ratio(a: str, b: str) -> float:
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def _merge(passages: list[_Seed]) -> list[_Seed]:
    """Merge passages on the same diagonal whose query ranges adjoin.

    Two passages merge only when they sit on the exact same diagonal
    (`c_start - q_start` equal) AND their query ranges overlap or sit within
    `_MERGE_GAP` chars. Same-diagonal means the two seeds describe one
    continuous shared run with a short mismatch in the middle. Different-diagonal
    seeds that happen to abut in query space describe *different* shared runs;
    merging them would synthesise an alignment whose middle does not align.
    """
    if not passages:
        return []
    sorted_passages = sorted(passages, key=lambda p: (p.q_start, p.c_start))
    merged: list[_Seed] = [sorted_passages[0]]
    for current in sorted_passages[1:]:
        last = merged[-1]
        same_diagonal = (current.c_start - current.q_start) == (last.c_start - last.q_start)
        overlap_or_adjacent = current.q_start <= last.q_end + _MERGE_GAP
        if same_diagonal and overlap_or_adjacent:
            merged[-1] = _Seed(
                q_start=last.q_start,
                q_end=max(last.q_end, current.q_end),
                c_start=last.c_start,
                c_end=max(last.c_end, current.c_end),
            )
        else:
            merged.append(current)
    return merged


def _dedupe_by_query_coverage(passages: list[_Seed]) -> list[_Seed]:
    """Drop passages whose query range overlaps a longer passage.

    Self-repetition on either side makes the seed stage emit several maximal
    exact matches over the same query region at different candidate offsets.
    From the report's perspective that is noise - the reader wants one
    attribution per query span. Keep the longest match per query region.
    """
    by_length_desc = sorted(passages, key=lambda p: -(p.q_end - p.q_start))
    kept: list[_Seed] = []
    for current in by_length_desc:
        current_len = current.q_end - current.q_start
        if current_len <= 0:
            continue
        subsumed = False
        for k in kept:
            overlap_start = max(current.q_start, k.q_start)
            overlap_end = min(current.q_end, k.q_end)
            overlap = max(0, overlap_end - overlap_start)
            if overlap / current_len > _QUERY_OVERLAP_DEDUPE_THRESHOLD:
                subsumed = True
                break
        if not subsumed:
            kept.append(current)
    return kept


def _score_and_build(
    span: _Seed,
    query_chunk_id: str,
    candidate_chunk_id: str,
    query_text: str,
    candidate_text: str,
    normalized_query: NormalizedText,
    normalized_candidate: NormalizedText,
) -> AlignedPassage:
    q_segment = normalized_query.text[span.q_start : span.q_end]
    c_segment = normalized_candidate.text[span.c_start : span.c_end]
    # The extend stage advances both sides in lock-step, so these have identical
    # length today. zip without strict= keeps the formula robust if a future
    # gap-tolerant extender lets them diverge.
    matches = sum(1 for a, b in zip(q_segment, c_segment) if a == b)  # noqa: B905
    aligned_len = max(len(q_segment), len(c_segment))
    query_start, query_end = normalized_query.original_span_with_boundaries(
        span.q_start, span.q_end, query_text
    )
    candidate_start, candidate_end = normalized_candidate.original_span_with_boundaries(
        span.c_start, span.c_end, candidate_text
    )
    if span.q_start == 0 and span.q_end == len(normalized_query.text):
        query_start, query_end = 0, len(query_text)
    if span.c_start == 0 and span.c_end == len(normalized_candidate.text):
        candidate_start, candidate_end = 0, len(candidate_text)
    return AlignedPassage(
        query_chunk_id=query_chunk_id,
        candidate_chunk_id=candidate_chunk_id,
        query_start=query_start,
        query_end=query_end,
        candidate_start=candidate_start,
        candidate_end=candidate_end,
        score=matches / aligned_len if aligned_len else 0.0,
        match_type="verbatim",
    )

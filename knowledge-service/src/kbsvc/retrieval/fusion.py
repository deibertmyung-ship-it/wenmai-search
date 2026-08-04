"""Reciprocal Rank Fusion.

Rank-based rather than score-based on purpose: dense cosine and BM25 dot
products are not on a comparable scale, and normalising them introduces its own
distortions. RRF only needs the ordering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..vector.base import SearchHit


@dataclass
class FusedHit:
    id: str
    score: float
    payload: dict = field(default_factory=dict)
    contributions: dict[str, int] = field(default_factory=dict)  # retriever -> rank (1-based)


def reciprocal_rank_fusion(
    runs: dict[str, list[SearchHit]],
    *,
    k: int = 60,
    weights: dict[str, float] | None = None,
    limit: int = 10,
) -> list[FusedHit]:
    weights = weights or {}
    fused: dict[str, FusedHit] = {}

    for retriever, hits in runs.items():
        weight = weights.get(retriever, 1.0)
        if weight == 0.0:
            continue
        for rank, hit in enumerate(hits, start=1):
            entry = fused.get(hit.id)
            if entry is None:
                entry = FusedHit(id=hit.id, score=0.0, payload=hit.payload)
                fused[hit.id] = entry
            elif not entry.payload:
                entry.payload = hit.payload
            entry.score += weight / (k + rank)
            entry.contributions[retriever] = rank

    ordered = sorted(fused.values(), key=lambda item: (-item.score, item.id))
    return ordered[:limit]

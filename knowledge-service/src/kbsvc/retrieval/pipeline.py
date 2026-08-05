"""Retrieval pipeline: rewrite -> embed -> search -> fuse -> rerank -> cite.

Shared verbatim by the REST API and the MCP server so the two can never drift.
Debug output is a first-class return value, not a log line - without it hybrid
retrieval is untunable.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Literal

from ..config import Settings, get_settings
from ..embedding import get_dense_embedder
from ..lexical import get_lexical_store
from ..vector import SearchFilter, get_vector_store
from ..vector.base import SearchHit
from .citation import build_snippet, source_anchor
from .fusion import reciprocal_rank_fusion
from .rerank import get_reranker
from .rewrite import rewrite

Mode = Literal["hybrid", "dense", "sparse"]


@dataclass
class RetrievalRequest:
    query: str
    tenant_id: str
    top_k: int = 10
    mode: Mode = "hybrid"
    acl: list[str] | None = None
    source_ids: list[str] | None = None
    document_ids: list[str] | None = None
    kinds: list[str] | None = None
    heading_contains: str | None = None
    current_only: bool = True
    rerank: bool = True
    rewrite: bool = True
    debug: bool = False


@dataclass
class RetrievalResult:
    chunk_id: str
    score: float
    rerank_score: float | None
    document_id: str
    version: int
    chunk_ordinal: int
    kind: str
    title: str
    heading_path: list[str]
    page: int | None
    source_uri: str
    snippet: str
    highlights: list[list[int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RetrievalResponse:
    query: str
    results: list[RetrievalResult]
    debug: dict | None = None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "results": [r.to_dict() for r in self.results],
            **({"debug": self.debug} if self.debug is not None else {}),
        }


class _Timer:
    def __init__(self) -> None:
        self.marks: dict[str, float] = {}
        self._start = time.perf_counter()
        self._last = self._start

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round((now - self._last) * 1000, 3)
        self._last = now

    def total(self) -> dict[str, float]:
        return {**self.marks, "total": round((time.perf_counter() - self._start) * 1000, 3)}


class RetrievalService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def search(self, request: RetrievalRequest) -> RetrievalResponse:
        timer = _Timer()
        queries = rewrite(request.query, enabled=request.rewrite)
        if not queries:
            return RetrievalResponse(query=request.query, results=[], debug=None)
        timer.mark("rewrite")

        flt = SearchFilter(
            tenant_id=request.tenant_id,
            acl_any=request.acl,
            source_ids=request.source_ids,
            document_ids=request.document_ids,
            kinds=request.kinds,
            current_only=request.current_only,
        )
        limit = max(request.top_k * self.settings.retrieval_overfetch, request.top_k)

        runs, timings = self._run_retrievers(queries, request.mode, limit, flt)
        timer.mark("search")  # close the search span before folding in per-retriever detail
        timer.marks.update(timings)

        weights = {
            "dense": self.settings.dense_weight if request.mode != "sparse" else 0.0,
            "sparse": self.settings.sparse_weight if request.mode != "dense" else 0.0,
        }
        fused = reciprocal_rank_fusion(
            runs, k=self.settings.rrf_k, weights=weights, limit=limit
        )
        timer.mark("fuse")

        candidates = self._apply_heading_filter(fused, request.heading_contains)
        results, rerank_scores = self._rank_and_cite(request, candidates)
        timer.mark("rerank")

        debug = None
        if request.debug:
            debug = {
                "rewritten_queries": queries,
                "retrievers": {
                    name: [
                        {"id": hit.id, "score": round(hit.score, 6), "rank": rank}
                        for rank, hit in enumerate(hits, start=1)
                    ]
                    for name, hits in runs.items()
                },
                "fusion": {
                    "method": "rrf",
                    "k": self.settings.rrf_k,
                    "weights": weights,
                    "order": [
                        {"id": hit.id, "score": round(hit.score, 6), "from": hit.contributions}
                        for hit in fused[: request.top_k]
                    ],
                },
                "rerank": {
                    "reranker": get_reranker().name if request.rerank else "disabled",
                    "scores": rerank_scores,
                },
                "filter": flt.describe(),
                "overfetch_limit": limit,
                "timings_ms": timer.total(),
            }

        return RetrievalResponse(query=request.query, results=results, debug=debug)

    # --- stages ---------------------------------------------------------

    def _run_retrievers(
        self, queries: list[str], mode: Mode, limit: int, flt: SearchFilter
    ) -> tuple[dict[str, list[SearchHit]], dict[str, float]]:
        # Timed separately: opening the default embedded collection is lazy and can
        # dominate the first query. Keep it visible in timing reconciliation.
        started = time.perf_counter()
        store = get_vector_store()
        runs: dict[str, list[SearchHit]] = {}
        timings: dict[str, float] = {
            "store_init": round((time.perf_counter() - started) * 1000, 3)
        }

        if mode in ("hybrid", "dense"):
            started = time.perf_counter()
            embedder = get_dense_embedder()
            hits: dict[str, SearchHit] = {}
            for query in queries:
                for hit in store.search_dense(embedder.embed_query(query), limit=limit, flt=flt):
                    if hit.id not in hits or hit.score > hits[hit.id].score:
                        hits[hit.id] = hit
            runs["dense"] = sorted(hits.values(), key=lambda h: -h.score)[:limit]
            timings["dense_search"] = round((time.perf_counter() - started) * 1000, 3)

        if mode in ("hybrid", "sparse"):
            started = time.perf_counter()
            # One lexical query for all rewrite variants, not one per variant.
            # `rewrite` only ever *removes* text - it strips a question tail, a
            # leading framing phrase, or splits on punctuation - so every variant
            # is a contiguous substring of the original and contributes no term
            # the original lacked. Joining them with a space (which the tokenizer
            # treats as a hard boundary, so no bigram spans two variants) yields
            # exactly the original query's term set.
            #
            # Dense deliberately keeps its per-variant searches: there the
            # variants embed to genuinely different points, which is where
            # rewriting earns its keep.
            runs["sparse"] = get_lexical_store().search(
                " ".join(queries), limit=limit, flt=flt
            )
            timings["sparse_search"] = round((time.perf_counter() - started) * 1000, 3)

        return runs, timings

    def _apply_heading_filter(self, fused, heading_contains: str | None):
        if not heading_contains:
            return fused
        needle = heading_contains.lower()
        return [
            hit
            for hit in fused
            if needle in " ".join(hit.payload.get("heading_path") or []).lower()
        ]

    def _rank_and_cite(self, request: RetrievalRequest, candidates) -> tuple[list, dict]:
        texts = [hit.payload.get("text", "") for hit in candidates]
        rerank_scores: dict[str, float] = {}
        if request.rerank and candidates:
            scores = get_reranker().score(request.query, texts)
            rerank_scores = {hit.id: score for hit, score in zip(candidates, scores, strict=True)}
            order = sorted(
                range(len(candidates)),
                key=lambda i: (-scores[i], -candidates[i].score),
            )
        else:
            order = list(range(len(candidates)))

        results: list[RetrievalResult] = []
        for index in order[: request.top_k]:
            hit = candidates[index]
            payload = hit.payload
            page = payload.get("page_from")
            snippet, highlights = build_snippet(
                payload.get("text", ""), request.query, width=self.settings.snippet_chars
            )
            results.append(
                RetrievalResult(
                    chunk_id=hit.id,
                    score=round(hit.score, 6),
                    rerank_score=rerank_scores.get(hit.id),
                    document_id=payload.get("document_id", ""),
                    version=int(payload.get("version", 0) or 0),
                    chunk_ordinal=int(payload.get("chunk_ordinal", 0) or 0),
                    kind=payload.get("kind", "text"),
                    title=payload.get("title", ""),
                    heading_path=payload.get("heading_path") or [],
                    page=page,
                    source_uri=source_anchor(payload.get("source_uri", ""), page),
                    snippet=snippet,
                    highlights=highlights,
                )
            )
        return results, {k: round(v, 6) for k, v in rerank_scores.items()}


_service: RetrievalService | None = None


def get_retrieval_service() -> RetrievalService:
    global _service
    if _service is None:
        _service = RetrievalService()
    return _service


def reset_retrieval_service() -> None:
    global _service
    _service = None

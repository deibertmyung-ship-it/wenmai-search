"""Unit tests for the retrieval building blocks."""

from __future__ import annotations

from kbsvc.embedding.dense_fastembed import _local_model_path
from kbsvc.embedding.dense_hash import HashDenseEmbedder
from kbsvc.lexical.tokenizer import analyze, tokenize
from kbsvc.retrieval.citation import build_snippet, source_anchor
from kbsvc.retrieval.fusion import reciprocal_rank_fusion
from kbsvc.retrieval.rerank import LexicalReranker, NoopReranker
from kbsvc.retrieval.rewrite import rewrite
from kbsvc.vector.base import SearchFilter, SearchHit

# --- lexical tokenizer --------------------------------------------------


def test_tokenize_emits_cjk_unigrams_and_bigrams():
    tokens = tokenize("贼克")
    assert "贼" in tokens and "克" in tokens and "贼克" in tokens


def test_tokenize_lowercases_latin_words():
    assert "hybrid" in tokenize("Hybrid Search")


def test_tokenize_does_not_bridge_a_whitespace_boundary():
    """The pipeline joins rewrite variants with a space and relies on this."""
    assert "克涉" not in tokenize("贼克 涉害")


def test_empty_text_tokenizes_to_nothing():
    assert tokenize("") == []
    assert analyze("") == ""


def test_analyze_is_the_whitespace_joined_token_stream():
    assert analyze("贼克") == " ".join(tokenize("贼克"))


# --- dense --------------------------------------------------------------


def test_hash_embedder_is_deterministic_and_normalized():
    embedder = HashDenseEmbedder(dim=64)
    first = embedder.embed_query("贼克取用")
    assert first == embedder.embed_query("贼克取用")
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_hash_embedder_handles_empty_input():
    assert HashDenseEmbedder(dim=8).embed_query("") == [0.0] * 8


def test_fastembed_detects_preloaded_local_model(tmp_path):
    model_dir = tmp_path / "fast-bge-small-zh-v1.5"
    model_dir.mkdir()
    (model_dir / "model_optimized.onnx").touch()

    assert _local_model_path(str(tmp_path), "BAAI/bge-small-zh-v1.5") == model_dir


# --- fusion -------------------------------------------------------------


def test_rrf_rewards_documents_found_by_both_retrievers():
    runs = {
        "dense": [SearchHit("a", 0.9), SearchHit("b", 0.8)],
        "sparse": [SearchHit("b", 5.0), SearchHit("c", 4.0)],
    }
    fused = reciprocal_rank_fusion(runs, k=60, limit=3)
    assert fused[0].id == "b"
    assert fused[0].contributions == {"dense": 2, "sparse": 1}


def test_rrf_respects_zero_weight_as_disabling_a_retriever():
    runs = {"dense": [SearchHit("a", 0.9)], "sparse": [SearchHit("b", 5.0)]}
    fused = reciprocal_rank_fusion(runs, weights={"dense": 0.0, "sparse": 1.0}, limit=5)
    assert [hit.id for hit in fused] == ["b"]


def test_rrf_is_deterministic_for_tied_scores():
    runs = {"dense": [SearchHit("b", 1.0)], "sparse": [SearchHit("a", 1.0)]}
    assert [hit.id for hit in reciprocal_rank_fusion(runs, limit=2)] == ["a", "b"]


# --- rerank -------------------------------------------------------------


def test_lexical_reranker_prefers_higher_query_coverage():
    scores = LexicalReranker().score(
        "贼克取用", ["贼克者取用之首法也", "此篇专论涉害与比用", ""]
    )
    assert scores[0] > scores[1]
    assert scores[2] == 0.0


def test_noop_reranker_returns_zeros():
    assert NoopReranker().score("q", ["a", "b"]) == [0.0, 0.0]


# --- rewrite ------------------------------------------------------------


def test_rewrite_keeps_the_original_query_first():
    variants = rewrite("请问什么是贼克？")
    assert variants[0] == "请问什么是贼克？"
    assert any(v == "贼克" for v in variants)


def test_rewrite_disabled_returns_only_the_original():
    assert rewrite("请问什么是贼克？", enabled=False) == ["请问什么是贼克？"]


def test_rewrite_splits_multi_intent_queries():
    variants = rewrite("贼克，涉害")
    assert "贼克" in variants and "涉害" in variants


def test_rewrite_handles_blank_input():
    assert rewrite("   ") == []


# --- citation -----------------------------------------------------------


def test_snippet_windows_around_the_densest_match():
    text = "前言" * 200 + "贼克者取用之首法也" + "后记" * 200
    snippet, highlights = build_snippet(text, "贼克", width=80)
    assert "贼克" in snippet
    assert highlights and snippet[highlights[0][0] : highlights[0][1]]


def test_snippet_marks_truncation_with_ellipses():
    snippet, _ = build_snippet("甲" * 500, "甲", width=50)
    assert snippet.endswith("…")


def test_snippet_of_short_text_is_returned_whole():
    snippet, _ = build_snippet("贼克", "贼克", width=100)
    assert snippet == "贼克"


def test_source_anchor_appends_a_page_fragment():
    assert source_anchor("file:///a.pdf", 7) == "file:///a.pdf#page=7"
    assert source_anchor("file:///a.pdf", None) == "file:///a.pdf"
    assert source_anchor("", 3) == ""


# --- pipeline: retriever dispatch ---------------------------------------

_EXPANDING_QUERY = "请问什么是贼克，以及涉害？"


class _CountingVectorStore:
    def __init__(self) -> None:
        self.dense_calls: int = 0

    def search_dense(self, vector, *, limit, flt) -> list[SearchHit]:
        self.dense_calls += 1
        return []


class _CountingLexicalStore:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query, *, limit, flt) -> list[SearchHit]:
        self.queries.append(query)
        return []


def _run(monkeypatch, tenant: str, mode: str, queries: list[str]):
    from kbsvc.retrieval import pipeline

    vector = _CountingVectorStore()
    lexical = _CountingLexicalStore()
    monkeypatch.setattr(pipeline, "get_vector_store", lambda: vector)
    monkeypatch.setattr(pipeline, "get_lexical_store", lambda: lexical)
    pipeline.RetrievalService()._run_retrievers(queries, mode, 40, SearchFilter(tenant_id=tenant))
    return vector, lexical


def test_rewrite_variants_cost_one_lexical_query_not_one_each(monkeypatch, tenant):
    queries = rewrite(_EXPANDING_QUERY)
    assert len(queries) > 1, "this query must actually expand or the test proves nothing"

    _vector, lexical = _run(monkeypatch, tenant, "sparse", queries)

    assert len(lexical.queries) == 1


def test_merged_lexical_query_adds_no_term_the_original_lacked(monkeypatch, tenant):
    """Variants are substrings of the original, so the union is the original's term set."""
    queries = rewrite(_EXPANDING_QUERY)
    _vector, lexical = _run(monkeypatch, tenant, "sparse", queries)

    assert set(tokenize(lexical.queries[0])) == set(tokenize(queries[0]))


def test_dense_still_searches_once_per_variant(monkeypatch, tenant):
    """Rewriting earns its keep on the dense side, where variants embed differently."""
    queries = rewrite(_EXPANDING_QUERY)
    vector, lexical = _run(monkeypatch, tenant, "dense", queries)

    assert vector.dense_calls == len(queries)
    assert lexical.queries == []

"""Unit tests for the retrieval building blocks."""

from __future__ import annotations

from kbsvc.embedding.dense_fastembed import _local_model_path
from kbsvc.embedding.dense_hash import HashDenseEmbedder
from kbsvc.embedding.sparse_bm25 import Bm25SparseEmbedder, StaticTermStats, term_index, tokenize
from kbsvc.retrieval.citation import build_snippet, source_anchor
from kbsvc.retrieval.fusion import reciprocal_rank_fusion
from kbsvc.retrieval.rerank import LexicalReranker, NoopReranker
from kbsvc.retrieval.rewrite import rewrite
from kbsvc.vector.base import SearchHit

# --- sparse -------------------------------------------------------------


def test_tokenize_emits_cjk_unigrams_and_bigrams():
    tokens = tokenize("贼克")
    assert "贼" in tokens and "克" in tokens and "贼克" in tokens


def test_tokenize_lowercases_latin_words():
    assert "hybrid" in tokenize("Hybrid Search")


def test_term_index_is_stable_and_in_range():
    assert term_index("贼克") == term_index("贼克")
    assert 0 <= term_index("贼克") < 2**31


def test_document_and_query_vectors_share_an_index_space():
    stats = StaticTermStats({"贼克": 2}, count=10, avg_len=20.0)
    embedder = Bm25SparseEmbedder(stats)
    document = embedder.encode_document("贼克者取用之首法也")
    query = embedder.encode_query("贼克")
    assert set(query) & set(document), "query terms must hit document indices"


def test_rarer_terms_get_higher_query_weight():
    stats = StaticTermStats({"的": 900, "贼克": 2}, count=1000, avg_len=20.0)
    query = Bm25SparseEmbedder(stats).encode_query("的 贼克")
    assert query[term_index("贼克")] > query[term_index("的")]


def test_empty_query_encodes_to_empty_vector():
    assert Bm25SparseEmbedder(StaticTermStats()).encode_query("") == {}


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

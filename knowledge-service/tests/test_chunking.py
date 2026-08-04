"""Chunking must stay traceable: offsets, heading paths, and section boundaries."""

from __future__ import annotations

from kbsvc.chunking.structural import StructuralChunker
from kbsvc.chunking.tokenizer import estimate_tokens, split_sentences
from kbsvc.models.ir import BBox, Block, DocumentMeta, ParsedDocument, Section, Table
from kbsvc.parsing.base import build_from_markdown


def test_estimate_tokens_scales_with_script():
    assert estimate_tokens("") == 0
    assert estimate_tokens("六壬贼克") == 4
    assert estimate_tokens("abcdefgh") == 2


def test_split_sentences_keeps_terminators():
    parts = split_sentences("甲为用。乙为神！丙如何？")
    assert len(parts) == 3
    assert parts[0].endswith("。")


def test_chunks_never_span_two_headings(sample_markdown):
    parsed = build_from_markdown(sample_markdown, parser="text", version="1")
    chunks = StructuralChunker().chunk(parsed)

    assert chunks, "expected at least one chunk"
    for chunk in chunks:
        # a chunk belongs to exactly one section, so its breadcrumb is unambiguous
        assert chunk.section_id
    paths = {tuple(chunk.heading_path) for chunk in chunks}
    assert any("卷一" in path for path in paths)
    assert any("卷二" in path for path in paths)


def test_ordinals_are_dense_and_hashes_are_populated(sample_markdown):
    parsed = build_from_markdown(sample_markdown, parser="text", version="1")
    chunks = StructuralChunker().chunk(parsed)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.content_hash for chunk in chunks)
    assert all(chunk.token_count > 0 for chunk in chunks)


def test_char_offsets_point_back_into_the_source_text(sample_markdown):
    parsed = build_from_markdown(sample_markdown, parser="text", version="1")
    for chunk in StructuralChunker().chunk(parsed):
        assert 0 <= chunk.char_start <= chunk.char_end <= len(parsed.text)


def test_oversized_unpunctuated_text_is_hard_split(settings):
    body = "壬" * (settings.chunk_max_chars * 2)
    parsed = ParsedDocument(
        meta=DocumentMeta(),
        text=body,
        sections=[Section(id="s0", char_start=0, char_end=len(body))],
        blocks=[Block(id="b0", text=body, section_id="s0", char_start=0, char_end=len(body))],
    )
    chunks = StructuralChunker().chunk(parsed)
    assert len(chunks) > 1
    assert all(len(chunk.text) <= settings.chunk_max_chars for chunk in chunks)


def test_tables_are_kept_whole_and_marked():
    parsed = ParsedDocument(
        meta=DocumentMeta(),
        text="",
        sections=[Section(id="s0", heading="表", heading_path=["表"])],
        blocks=[],
        tables=[
            Table(
                id="t0",
                section_id="s0",
                page=3,
                bbox=BBox(page=3, left=1, top=2, right=3, bottom=4),
                markdown="| a | b |\n| - | - |\n| 1 | 2 |",
            )
        ],
    )
    chunks = StructuralChunker().chunk(parsed)
    assert len(chunks) == 1
    assert chunks[0].kind == "table"
    assert chunks[0].page_from == 3
    assert chunks[0].bbox == [[3.0, 1.0, 2.0, 3.0, 4.0]]
    assert chunks[0].heading_path == ["表"]


def test_runt_chunks_are_absorbed_into_their_neighbour(settings):
    long_body = "贼克者取用之首法也。" * 12
    parsed = ParsedDocument(
        meta=DocumentMeta(),
        text=long_body + "短。",
        sections=[Section(id="s0")],
        blocks=[
            Block(id="b0", text=long_body, section_id="s0", char_start=0, char_end=len(long_body)),
            Block(
                id="b1",
                text="短。",
                section_id="s0",
                char_start=len(long_body),
                char_end=len(long_body) + 2,
            ),
        ],
    )
    chunks = StructuralChunker().chunk(parsed)
    assert all(
        len(chunk.text) >= settings.chunk_min_chars or chunk is chunks[0] for chunk in chunks
    )
    assert "短。" in "".join(chunk.text for chunk in chunks)

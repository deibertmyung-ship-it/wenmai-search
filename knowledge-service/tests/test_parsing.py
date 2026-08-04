"""Parser behaviour: encoding detection, heading promotion, fallback chain."""

from __future__ import annotations

import pytest

from kbsvc.errors import ParserError
from kbsvc.parsing.base import build_from_markdown, normalize_text
from kbsvc.parsing.registry import ParserRegistry, guess_mime
from kbsvc.parsing.text_parser import TextParser, decode_bytes


@pytest.mark.parametrize(
    ("encoding", "prefix"),
    [("utf-16", b""), ("utf-8-sig", b""), ("utf-8", b""), ("gb18030", b"")],
)
def test_decode_bytes_handles_corpus_encodings(encoding, prefix):
    text = "六壬贼克，取用之首法也。"
    assert decode_bytes(prefix + text.encode(encoding)) == text


def test_decode_bytes_never_raises_on_garbage():
    assert isinstance(decode_bytes(b"\x81\x82\x83\xff\xfa"), str)


def test_text_parser_promotes_cjk_headings_to_sections():
    raw = "　　卷一\n　　贼克者，取用之首法也。\n　　第二章\n　　涉害者，比用不成则涉害。\n"
    parsed = TextParser().parse(raw.encode("utf-16"), filename="六壬指南.txt", mime="text/plain")

    headings = [section.heading for section in parsed.sections if section.heading]
    assert "卷一" in headings
    assert "第二章" in headings
    assert parsed.meta.lang == "zh"
    assert parsed.meta.title == "六壬指南"


def test_text_parser_does_not_treat_prose_as_a_heading():
    raw = "凡四课之中，有一下贼上者，即取之为用神，此为定法。\n"
    parsed = TextParser().parse(raw.encode("utf-8"), filename="a.txt", mime="text/plain")
    assert [s.heading for s in parsed.sections if s.heading] == []


def test_build_from_markdown_tracks_heading_path():
    parsed = build_from_markdown(
        "# Book\n\nintro\n\n## Part\n\nbody\n\n### Sub\n\ndeep\n",
        parser="text",
        version="1.0.0",
        title="Book",
    )
    paths = [section.heading_path for section in parsed.sections]
    assert ["Book", "Part", "Sub"] in paths or ["Part", "Sub"] in paths
    deepest = max(parsed.sections, key=lambda s: len(s.heading_path))
    assert deepest.heading_path[-1] == "Sub"


def test_blocks_carry_offsets_into_the_normalized_text():
    parsed = build_from_markdown("# T\n\nalpha beta\n\n## S\n\ngamma\n", parser="text", version="1")
    for block in parsed.blocks:
        assert parsed.text[block.char_start : block.char_end].strip()[:5] == block.text[:5]


def test_normalize_text_collapses_blank_runs_and_line_endings():
    assert normalize_text("a\r\n\r\n\r\n\r\nb") == "a\n\nb"


def test_registry_prefers_text_parser_for_text_inputs():
    registry = ParserRegistry(chain=["docling", "unstructured"])
    assert [p.name for p in registry.candidates("a.txt", "text/plain")] == ["text"]


def test_registry_reports_every_attempt_when_all_parsers_fail():
    class Boom:
        name = "boom"
        version = "0"

        def supports(self, mime: str, suffix: str) -> bool:
            return True

        def parse(self, data, *, filename, mime):
            raise RuntimeError("nope")

    registry = ParserRegistry(chain=[])
    registry._chain = [Boom()]
    with pytest.raises(ParserError) as excinfo:
        registry.parse(b"data", filename="a.pdf")
    assert "boom" in str(excinfo.value.detail["attempts"])


def test_guess_mime_falls_back_to_octet_stream():
    assert guess_mime("a.txt") == "text/plain"
    assert guess_mime("a.unknown-ext") == "application/octet-stream"

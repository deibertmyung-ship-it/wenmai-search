"""Parser protocol plus helpers shared by every concrete parser."""

from __future__ import annotations

import re
from typing import Protocol

from ..models.ir import Block, ParsedDocument, Section

_WS_RUNS = re.compile(r"[ \t　]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


class Parser(Protocol):
    name: str
    version: str

    def supports(self, mime: str, suffix: str) -> bool: ...

    def parse(self, data: bytes, *, filename: str, mime: str) -> ParsedDocument: ...


def normalize_text(text: str) -> str:
    """Normalize line endings and collapse runaway whitespace, preserving structure."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = _WS_RUNS.sub(" ", text)
    text = _BLANK_RUNS.sub("\n\n", text)
    return text.strip()


def build_from_markdown(
    markdown: str, *, parser: str, version: str, title: str = "", page_count: int | None = None
) -> ParsedDocument:
    """Turn markdown (docling/marker/unstructured all emit it) into the IR.

    Sections come from ATX headings; the running heading_path gives every block
    its breadcrumb, which is what makes a retrieved chunk traceable.
    """
    from ..models.ir import DocumentMeta

    text = normalize_text(markdown)
    sections: list[Section] = []
    blocks: list[Block] = []
    path: list[str] = []
    levels: list[int] = []

    # The root section has no heading of its own; the document title lives in meta,
    # so heading_path only ever contains real headings.
    root = Section(id="s0", level=0, heading="", heading_path=[], char_start=0, char_end=len(text))
    sections.append(root)
    current = root
    cursor = 0
    buffer: list[str] = []
    buffer_start = 0

    def flush_buffer() -> None:
        nonlocal buffer
        raw = "\n".join(buffer)
        body = raw.strip()
        if body:
            # Offsets must survive stripping, or citations point at the wrong span.
            lead = len(raw) - len(raw.lstrip())
            start = buffer_start + lead
            blocks.append(
                Block(
                    id=f"b{len(blocks)}",
                    kind="text",
                    text=body,
                    section_id=current.id,
                    char_start=start,
                    char_end=start + len(body),
                )
            )
        buffer = []

    for line in text.split("\n"):
        line_start = cursor
        cursor += len(line) + 1
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not heading_match:
            if not buffer:
                if not line.strip():
                    continue  # don't anchor a block on leading blank lines
                buffer_start = line_start
            buffer.append(line)
            continue

        flush_buffer()
        current.char_end = line_start
        level = len(heading_match.group(1))
        heading = heading_match.group(2).strip()
        while levels and levels[-1] >= level:
            levels.pop()
            path.pop()
        levels.append(level)
        path.append(heading)
        current = Section(
            id=f"s{len(sections)}",
            level=level,
            heading=heading,
            heading_path=list(path),
            char_start=line_start,
            char_end=len(text),
        )
        sections.append(current)

    flush_buffer()

    return ParsedDocument(
        meta=DocumentMeta(
            title=title, parser=parser, parser_version=version, page_count=page_count
        ),
        text=text,
        sections=sections,
        blocks=blocks,
    )

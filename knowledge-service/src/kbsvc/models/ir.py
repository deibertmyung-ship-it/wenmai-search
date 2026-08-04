"""Parser-independent intermediate representation.

Every parser (docling / unstructured / marker / plain text) must produce a
ParsedDocument. Downstream chunking and indexing know nothing about the parser.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BlockKind = Literal["text", "table", "figure", "code", "list"]


class BBox(BaseModel):
    """Page-anchored bounding box; l/t/r/b in the parser's own coordinate space."""

    page: int
    left: float
    top: float
    right: float
    bottom: float

    def as_tuple(self) -> list[float]:
        return [float(self.page), self.left, self.top, self.right, self.bottom]


class Section(BaseModel):
    id: str
    level: int = 1
    heading: str = ""
    heading_path: list[str] = Field(default_factory=list)
    page: int | None = None
    char_start: int = 0
    char_end: int = 0


class Block(BaseModel):
    id: str
    kind: BlockKind = "text"
    text: str = ""
    section_id: str = ""
    page: int | None = None
    char_start: int = 0
    char_end: int = 0
    bbox: BBox | None = None


class Table(BaseModel):
    id: str
    section_id: str = ""
    page: int | None = None
    bbox: BBox | None = None
    markdown: str = ""
    rows: list[list[str]] = Field(default_factory=list)


class DocumentMeta(BaseModel):
    title: str = ""
    lang: str = "zh"
    page_count: int | None = None
    parser: str = "text"
    parser_version: str = "0.0.0"
    extra: dict = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    """Normalized output of any parser."""

    meta: DocumentMeta
    text: str = ""  # full normalized text; char offsets below index into this
    sections: list[Section] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)

    def section_by_id(self, section_id: str) -> Section | None:
        for section in self.sections:
            if section.id == section_id:
                return section
        return None


class Chunk(BaseModel):
    """A retrievable unit with full provenance back to the source document."""

    ordinal: int
    kind: BlockKind = "text"
    text: str
    token_count: int = 0
    char_start: int = 0
    char_end: int = 0
    page_from: int | None = None
    page_to: int | None = None
    section_id: str = ""
    heading_path: list[str] = Field(default_factory=list)
    bbox: list[list[float]] = Field(default_factory=list)
    content_hash: str = ""

    @property
    def heading(self) -> str:
        return self.heading_path[-1] if self.heading_path else ""

"""Structure-aware chunker.

Rules, in priority order:
  1. never merge across a heading boundary
  2. tables stay whole
  3. inside a section, pack sentences up to the token target with overlap
  4. absorb runt chunks into their neighbour instead of emitting fragments

Every emitted chunk keeps char offsets into ParsedDocument.text plus its
heading_path / page / bbox, which is what makes a citation checkable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings, get_settings
from ..ids import hash_text
from ..models.ir import Block, Chunk, ParsedDocument, Section
from .tokenizer import chars_for_tokens, estimate_tokens, split_sentences


@dataclass(frozen=True)
class _Piece:
    text: str
    char_start: int
    char_end: int
    page: int | None
    bbox: list[float] | None


class StructuralChunker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def chunk(self, document: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        for block in document.blocks:
            section = document.section_by_id(block.section_id)
            chunks.extend(self._chunk_block(block, section))
        for table in document.tables:
            section = document.section_by_id(table.section_id)
            chunks.append(self._table_chunk(table, section))

        merged = self._absorb_runts(chunks)
        for ordinal, chunk in enumerate(merged):
            chunk.ordinal = ordinal
            chunk.content_hash = hash_text(chunk.text)
            chunk.token_count = estimate_tokens(chunk.text)
        return merged

    # --- internals ------------------------------------------------------

    def _chunk_block(self, block: Block, section: Section | None) -> list[Chunk]:
        text = block.text.strip()
        if not text:
            return []

        target_chars = chars_for_tokens(self.settings.chunk_target_tokens, text)
        target_chars = min(target_chars, self.settings.chunk_max_chars)
        overlap_chars = chars_for_tokens(self.settings.chunk_overlap_tokens, text)
        bbox = [block.bbox.as_tuple()] if block.bbox else []

        pieces = self._pack(block, target_chars)
        chunks: list[Chunk] = []
        for index, piece in enumerate(pieces):
            body = piece.text
            start = piece.char_start
            if index > 0 and overlap_chars > 0:
                previous = pieces[index - 1].text
                tail = previous[-overlap_chars:]
                body = f"{tail}{body}"
                start = max(start - len(tail), 0)
            chunks.append(
                Chunk(
                    ordinal=0,
                    kind="text",
                    text=body,
                    char_start=start,
                    char_end=piece.char_end,
                    page_from=block.page,
                    page_to=block.page,
                    section_id=block.section_id,
                    heading_path=list(section.heading_path) if section else [],
                    bbox=bbox,
                )
            )
        return chunks

    def _pack(self, block: Block, target_chars: int) -> list[_Piece]:
        """Greedily fill sentences up to target_chars, tracking absolute offsets."""
        pieces: list[_Piece] = []
        buffer = ""
        buffer_start = block.char_start
        cursor = block.char_start

        for sentence in split_sentences(block.text):
            if buffer and len(buffer) + len(sentence) > target_chars:
                pieces.append(
                    _Piece(buffer.strip(), buffer_start, cursor, block.page, None)
                )
                buffer = ""
                buffer_start = cursor
            if len(sentence) > target_chars:
                # A single oversized sentence (unpunctuated classical prose): hard-split.
                for offset in range(0, len(sentence), target_chars):
                    slice_ = sentence[offset : offset + target_chars]
                    pieces.append(
                        _Piece(
                            slice_.strip(),
                            cursor + offset,
                            cursor + offset + len(slice_),
                            block.page,
                            None,
                        )
                    )
                cursor += len(sentence)
                buffer_start = cursor
                continue
            buffer += sentence
            cursor += len(sentence)

        if buffer.strip():
            pieces.append(_Piece(buffer.strip(), buffer_start, cursor, block.page, None))
        return [piece for piece in pieces if piece.text]

    def _table_chunk(self, table, section: Section | None) -> Chunk:
        body = table.markdown or "\n".join("\t".join(row) for row in table.rows)
        return Chunk(
            ordinal=0,
            kind="table",
            text=body,
            page_from=table.page,
            page_to=table.page,
            section_id=table.section_id,
            heading_path=list(section.heading_path) if section else [],
            bbox=[table.bbox.as_tuple()] if table.bbox else [],
        )

    def _absorb_runts(self, chunks: list[Chunk]) -> list[Chunk]:
        """Merge sub-minimum chunks into the previous chunk of the same section."""
        minimum = self.settings.chunk_min_chars
        result: list[Chunk] = []
        for chunk in chunks:
            too_small = len(chunk.text) < minimum and chunk.kind == "text"
            same_section = bool(result) and result[-1].section_id == chunk.section_id
            can_merge = (
                too_small
                and same_section
                and result[-1].kind == "text"
                and len(result[-1].text) + len(chunk.text) <= self.settings.chunk_max_chars
            )
            if can_merge:
                previous = result[-1]
                previous.text = f"{previous.text}\n{chunk.text}"
                previous.char_end = max(previous.char_end, chunk.char_end)
                previous.page_to = chunk.page_to or previous.page_to
                previous.bbox = [*previous.bbox, *chunk.bbox]
                continue
            result.append(chunk)
        return result

"""Sliding-sentence chunking for plagiarism corpus projections.

Splits a document into overlapping multi-sentence chunks for L1 retrieval. Each
chunk's text is the exact substring of the input - char offsets are preserved so
matches can be reported against the original text and the alignment stage can
extend a match without re-slicing.

Sentence segmentation uses pysbd (rule-based, multilingual, no model download).
The window slides over sentences, not raw characters, so chunk boundaries
respect document structure even on noisy input.

Performance note: pysbd's `.segment()` is linear-ish on documents with natural
paragraph structure but degrades super-linearly on long continuous text with no
paragraph boundaries. Classical Chinese source files in this corpus are often
exactly that shape, so projection build time is dominated by segmentation, not
by fingerprinting.

Ported from noplag-engine `src/noplag_engine/chunking/sliding.py` at commit
005da60faad21bf52702997d73583b78d8905d22, Apache-2.0 (see
`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`).
Modified for kbsvc: chunk window parameters are supplied by the caller from
settings instead of defaulting here, and the dataclass is renamed to
`ProjectionChunk` to avoid colliding with the knowledge-base `Chunk` domain
type, which is a different thing entirely. Segmentation and windowing are
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectionChunk:
    chunk_index: int
    char_start: int
    char_end: int
    text: str
    sentence_count: int


def chunk_document(
    text: str,
    *,
    sentences_per_chunk: int,
    overlap: int,
    language: str = "en",
) -> list[ProjectionChunk]:
    """Split `text` into overlapping sentence windows.

    With `sentences_per_chunk=4, overlap=1`, chunks span sentence indices
    [0:4], [3:7], [6:10] and so on. The final window handles the remainder; a
    smaller `sentence_count` there is expected and not padded.

    `text` is not normalized - char offsets refer to the original input.

    Raises `ValueError` if `overlap >= sentences_per_chunk`, which would leave
    the window unable to advance.
    """
    if overlap >= sentences_per_chunk:
        msg = (
            f"overlap ({overlap}) must be strictly less than sentences_per_chunk "
            f"({sentences_per_chunk}); otherwise the sliding window cannot advance "
            f"and would loop or duplicate chunks."
        )
        raise ValueError(msg)
    if not text:
        return []

    import pysbd

    segmenter = pysbd.Segmenter(language=language, clean=False, char_span=True)
    sentences = segmenter.segment(text)
    if not sentences:
        return []

    stride = sentences_per_chunk - overlap
    chunks: list[ProjectionChunk] = []
    start = 0
    chunk_index = 0
    n = len(sentences)
    while start < n:
        end = min(start + sentences_per_chunk, n)
        span = sentences[start:end]
        char_start = span[0].start
        char_end = span[-1].end
        chunks.append(
            ProjectionChunk(
                chunk_index=chunk_index,
                char_start=char_start,
                char_end=char_end,
                text=text[char_start:char_end],
                sentence_count=len(span),
            )
        )
        chunk_index += 1
        # Stop once the window has covered the final sentence - emitting a
        # smaller tail chunk would only duplicate sentences already captured by
        # the previous window's overlap region.
        if end >= n:
            break
        start += stride
    return chunks

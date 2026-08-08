"""Shared, reversible text normalization for plagiarism matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

NORMALIZER_VERSION = "plag-normalizer-v2"

_CITATION_MARKER_RE = re.compile(
    r"\[\s*(?:"
    r"\d{1,4}"
    r"|citation needed|clarification needed|verification needed|page needed"
    r"|note\s*\d*|nb\s*\d*|dead link|sic|update|edit"
    r"|by whom|according to whom|when|who|why|where"
    r")\??\s*\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedText:
    """A matching representation and its mapping to the original text."""

    text: str
    original_starts: tuple[int, ...]
    original_ends: tuple[int, ...]

    @property
    def effective_chars(self) -> int:
        """Count meaningful characters, excluding whitespace and punctuation."""
        return sum(
            not ch.isspace() and not unicodedata.category(ch).startswith("P")
            for ch in self.text
        )

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a normalized half-open span back to original coordinates."""
        if not 0 <= start <= end <= len(self.text):
            raise ValueError(f"invalid normalized span [{start}, {end})")
        if start == end:
            if start == len(self.text):
                boundary = self.original_ends[-1] if self.original_ends else 0
            else:
                boundary = self.original_starts[start] if self.text else 0
            return boundary, boundary
        return self.original_starts[start], self.original_ends[end - 1]

    def original_span_with_boundaries(
        self, start: int, end: int, original_text: str
    ) -> tuple[int, int]:
        """Include ignored punctuation/whitespace at a match boundary."""
        mapped_start, mapped_end = self.original_span(start, end)
        if start == 0:
            mapped_start = 0
        elif mapped_start:
            index = mapped_start - 1
            while index >= 0 and _is_ignored_boundary(original_text[index]):
                mapped_start = index
                index -= 1

        if end == len(self.text):
            mapped_end = len(original_text)
        elif mapped_end < len(original_text):
            boundary = self.original_starts[end]
            while mapped_end < boundary and _is_ignored_boundary(original_text[mapped_end]):
                mapped_end += 1
        return mapped_start, mapped_end


@dataclass(frozen=True)
class _MappedChar:
    char: str
    start: int
    end: int


def _is_ignored_boundary(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("P")


def _nfkc_chars(text: str) -> list[_MappedChar]:
    chars: list[_MappedChar] = []
    for index, original in enumerate(text):
        expanded = unicodedata.normalize("NFKC", original)
        chars.extend(_MappedChar(char, index, index + 1) for char in expanded)
    return chars


def _remove_citations(chars: list[_MappedChar]) -> list[_MappedChar]:
    if not chars:
        return chars
    value = "".join(item.char for item in chars)
    removed: set[int] = set()
    for match in _CITATION_MARKER_RE.finditer(value):
        removed.update(range(match.start(), match.end()))
    return [item for index, item in enumerate(chars) if index not in removed]


def _lower(chars: list[_MappedChar]) -> list[_MappedChar]:
    lowered: list[_MappedChar] = []
    for item in chars:
        expanded = item.char.lower()
        lowered.extend(_MappedChar(char, item.start, item.end) for char in expanded)
    return lowered


def _collapse_generic_whitespace(chars: list[_MappedChar]) -> list[_MappedChar]:
    result: list[_MappedChar] = []
    index = 0
    while index < len(chars):
        item = chars[index]
        if not item.char.isspace():
            result.append(item)
            index += 1
            continue
        end = item.end
        index += 1
        while index < len(chars) and chars[index].char.isspace():
            end = chars[index].end
            index += 1
        result.append(_MappedChar(" ", item.start, end))
    return result


def normalize_with_offsets(text: str, *, profile: str = "generic") -> NormalizedText:
    """Normalize `text` while retaining a mapping for every output character."""
    if profile not in {"generic", "zh"}:
        raise ValueError(f"unknown normalization profile: {profile}")

    chars = _lower(_remove_citations(_nfkc_chars(text)))
    if profile == "generic":
        chars = _collapse_generic_whitespace(chars)
    else:
        chars = [
            item
            for item in chars
            if not item.char.isspace()
            and not unicodedata.category(item.char).startswith("P")
        ]

    return NormalizedText(
        text="".join(item.char for item in chars),
        original_starts=tuple(item.start for item in chars),
        original_ends=tuple(item.end for item in chars),
    )

"""Parser selection and fallback chain.

Plain text always wins for text-ish inputs (no reason to boot a model for a
.txt). Everything else walks KB_PARSER_CHAIN in order until one succeeds; the
parser that actually produced the output is recorded on the version row.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from ..config import get_settings
from ..errors import ParserError
from ..models.ir import ParsedDocument
from .base import Parser
from .docling_parser import DoclingParser
from .marker_parser import MarkerParser
from .text_parser import TextParser
from .unstructured_parser import UnstructuredParser

logger = logging.getLogger(__name__)

_BUILDERS: dict[str, type] = {
    "text": TextParser,
    "docling": DoclingParser,
    "unstructured": UnstructuredParser,
    "marker": MarkerParser,
}


class ParserRegistry:
    def __init__(self, chain: list[str] | None = None) -> None:
        names = chain if chain is not None else get_settings().parser_chain
        self._text = TextParser()
        self._chain: list[Parser] = []
        for name in names:
            builder = _BUILDERS.get(name)
            if builder is None:
                logger.warning("unknown parser %r in chain, skipping", name)
                continue
            if name == "text":
                continue  # text is always tried first for text-ish inputs
            self._chain.append(builder())

    def candidates(self, filename: str, mime: str) -> list[Parser]:
        suffix = Path(filename).suffix.lower()
        if self._text.supports(mime, suffix):
            return [self._text]
        chosen = [p for p in self._chain if p.supports(mime, suffix)]
        return chosen or [*self._chain, self._text]

    def parse(self, data: bytes, *, filename: str, mime: str = "") -> ParsedDocument:
        resolved_mime = mime or guess_mime(filename)
        errors: list[str] = []
        for parser in self.candidates(filename, resolved_mime):
            try:
                return parser.parse(data, filename=filename, mime=resolved_mime)
            except Exception as exc:
                errors.append(f"{parser.name}: {exc}")
                logger.warning("parser %s failed for %s: %s", parser.name, filename, exc)
        raise ParserError(
            f"no parser could handle {filename}", {"mime": resolved_mime, "attempts": errors}
        )


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


_registry: ParserRegistry | None = None


def get_registry() -> ParserRegistry:
    global _registry
    if _registry is None:
        _registry = ParserRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None

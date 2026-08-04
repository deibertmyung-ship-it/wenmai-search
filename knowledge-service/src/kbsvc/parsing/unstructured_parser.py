"""Unstructured fallback parser. Optional dependency: kbsvc[unstructured]."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..errors import DependencyMissingError, ParserError
from ..models.ir import ParsedDocument
from .base import build_from_markdown
from .docling_parser import SUFFIXES as _RICH_SUFFIXES

SUFFIXES = _RICH_SUFFIXES | {".doc", ".ppt", ".xls", ".epub", ".eml", ".msg", ".rtf", ".odt"}

_TITLE_CATEGORIES = {"Title", "Header", "SectionHeader"}


class UnstructuredParser:
    name = "unstructured"

    def __init__(self) -> None:
        self.version = "unavailable"

    def supports(self, mime: str, suffix: str) -> bool:
        return suffix.lower() in SUFFIXES

    def parse(self, data: bytes, *, filename: str, mime: str) -> ParsedDocument:
        try:
            from unstructured.partition.auto import partition
        except ImportError as exc:  # pragma: no cover - broken deployment
            raise DependencyMissingError(
                "unstructured is not installed; reinstall kbsvc and verify deployment dependencies"
            ) from exc
        try:
            from importlib.metadata import version as pkg_version

            self.version = pkg_version("unstructured")
        except Exception:  # pragma: no cover
            self.version = "unknown"

        suffix = Path(filename).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        try:
            elements = partition(filename=str(temp_path))
        except Exception as exc:  # pragma: no cover - external document formats
            raise ParserError(f"unstructured failed to parse {filename}: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        lines: list[str] = []
        pages: set[int] = set()
        for element in elements:
            text = (getattr(element, "text", "") or "").strip()
            if not text:
                continue
            category = getattr(element, "category", "") or type(element).__name__
            metadata = getattr(element, "metadata", None)
            page = getattr(metadata, "page_number", None) if metadata else None
            if page:
                pages.add(int(page))
            lines.append(f"## {text}" if category in _TITLE_CATEGORIES else text)

        return build_from_markdown(
            "\n\n".join(lines),
            parser=self.name,
            version=self.version,
            title=Path(filename).stem,
            page_count=max(pages) if pages else None,
        )

"""Docling parser - the default for PDF/DOCX/PPTX/XLSX/HTML/images.

Optional dependency: `pip install kbsvc[docling]`. When absent, the registry
simply skips it and moves down the fallback chain.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..errors import DependencyMissingError, ParserError
from ..models.ir import BBox, ParsedDocument
from .base import build_from_markdown

SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".xhtml",
    ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".webp", ".adoc",
}


class DoclingParser:
    name = "docling"

    def __init__(self) -> None:
        self._converter = None
        self.version = "unavailable"

    def supports(self, mime: str, suffix: str) -> bool:
        return suffix.lower() in SUFFIXES

    def _ensure_converter(self):
        if self._converter is not None:
            return self._converter
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:  # pragma: no cover - broken deployment
            raise DependencyMissingError(
                "docling is not installed; reinstall kbsvc and verify deployment dependencies"
            ) from exc
        try:
            from importlib.metadata import version as pkg_version

            self.version = pkg_version("docling")
        except Exception:  # pragma: no cover - metadata is best effort
            self.version = "unknown"
        self._converter = DocumentConverter()
        return self._converter

    def parse(self, data: bytes, *, filename: str, mime: str) -> ParsedDocument:
        converter = self._ensure_converter()
        suffix = Path(filename).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        try:
            result = converter.convert(str(temp_path))
            doc = result.document
            markdown = doc.export_to_markdown()
        except Exception as exc:  # pragma: no cover - depends on optional extra
            raise ParserError(f"docling failed to convert {filename}: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        page_count = len(getattr(doc, "pages", {}) or {}) or None
        parsed = build_from_markdown(
            markdown,
            parser=self.name,
            version=self.version,
            title=Path(filename).stem,
            page_count=page_count,
        )
        _attach_page_anchors(parsed, doc)
        return parsed


def _attach_page_anchors(parsed: ParsedDocument, doc) -> None:
    """Best-effort mapping from docling provenance onto our blocks.

    Docling gives page + bbox per item; we align by matching item text against
    block text so a retrieved chunk can still cite a page and a box.
    """
    items = list(getattr(doc, "texts", []) or [])
    if not items:
        return
    anchors: list[tuple[str, int, BBox | None]] = []
    for item in items:
        text = (getattr(item, "text", "") or "").strip()
        provenance = list(getattr(item, "prov", []) or [])
        if not text or not provenance:
            continue
        prov = provenance[0]
        page = int(getattr(prov, "page_no", 0) or 0)
        bbox = getattr(prov, "bbox", None)
        box = None
        if bbox is not None:
            box = BBox(
                page=page,
                left=float(getattr(bbox, "l", 0.0)),
                top=float(getattr(bbox, "t", 0.0)),
                right=float(getattr(bbox, "r", 0.0)),
                bottom=float(getattr(bbox, "b", 0.0)),
            )
        anchors.append((text[:80], page, box))

    for block in parsed.blocks:
        head = block.text.strip()[:80]
        for text, page, box in anchors:
            if head and (head.startswith(text[:24]) or text.startswith(head[:24])):
                block.page = page
                block.bbox = box
                break

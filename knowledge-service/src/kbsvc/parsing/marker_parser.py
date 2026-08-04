"""Marker fallback parser for high-fidelity PDF. Optional dependency: kbsvc[marker]."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..errors import DependencyMissingError, ParserError
from ..models.ir import ParsedDocument
from .base import build_from_markdown

SUFFIXES = {".pdf"}


class MarkerParser:
    name = "marker"

    def __init__(self) -> None:
        self._converter = None
        self.version = "unavailable"

    def supports(self, mime: str, suffix: str) -> bool:
        return suffix.lower() in SUFFIXES

    def parse(self, data: bytes, *, filename: str, mime: str) -> ParsedDocument:
        try:
            from marker.converters.pdf import PdfConverter
            from marker.models import create_model_dict
            from marker.output import text_from_rendered
        except ImportError as exc:  # pragma: no cover - optional extra
            raise DependencyMissingError(
                "marker-pdf is not installed; install kbsvc[marker]"
            ) from exc
        try:
            from importlib.metadata import version as pkg_version

            self.version = pkg_version("marker-pdf")
        except Exception:  # pragma: no cover
            self.version = "unknown"

        if self._converter is None:
            self._converter = PdfConverter(artifact_dict=create_model_dict())

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(data)
            temp_path = Path(handle.name)
        try:
            rendered = self._converter(str(temp_path))
            markdown, _, _ = text_from_rendered(rendered)
        except Exception as exc:  # pragma: no cover - optional extra
            raise ParserError(f"marker failed to parse {filename}: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        return build_from_markdown(
            markdown, parser=self.name, version=self.version, title=Path(filename).stem
        )

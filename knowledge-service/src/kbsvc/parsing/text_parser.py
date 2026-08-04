"""Plain text / markdown parser.

Zero dependencies, so the pipeline is always runnable. It also carries the
Chinese-classics heuristics this deployment needs: the corpus is mostly UTF-16
`.txt` with 卷/篇/章 headings rather than markdown.
"""

from __future__ import annotations

import re

from ..models.ir import DocumentMeta, ParsedDocument
from .base import build_from_markdown, normalize_text

VERSION = "1.1.0"

# Order matters. Bare "utf-16" is NOT in this ladder: without a BOM it decodes
# almost any even-length byte string into garbage instead of raising, which
# would silently corrupt GB18030 text. BOM-less UTF-16 is detected separately.
_ENCODINGS = ("utf-8", "gb18030", "big5", "latin-1")
_NUL_RATIO_FOR_UTF16 = 0.2

# 卷一 / 第三章 / 卷之五 / 上卷 / 序 / 跋 / 目录 / 附录 ...
_CJK_HEADING = re.compile(
    r"^("
    r"(?:第[〇零一二三四五六七八九十百千0-9]+[章节節回卷篇部集])"
    r"|(?:卷(?:之)?[〇零一二三四五六七八九十百千0-9]+)"
    r"|(?:[上中下]卷)"
    r"|(?:[〇零一二三四五六七八九十百千0-9]+[、.．]\s*\S{0,20})"
    r"|(?:序|自序|原序|跋|后记|後記|目录|目錄|凡例|附录|附錄|总论|總論)"
    r")\s*\S{0,24}$"
)
_MAX_HEADING_CHARS = 32


def decode_bytes(data: bytes) -> str:
    """Decode with BOM-first detection; the corpus is predominantly UTF-16."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")

    # BOM-less UTF-16: CJK text in UTF-16 is roughly half NUL bytes in one lane.
    sample = data[:4096]
    if sample and sample.count(0) / len(sample) > _NUL_RATIO_FOR_UTF16:
        for encoding in ("utf-16-le", "utf-16-be"):
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue

    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip().strip("　")
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return False
    if stripped.endswith(("。", "，", "；", "：", "、", ".", ",", ";")):
        return False
    return bool(_CJK_HEADING.match(stripped))


def to_markdown(text: str) -> str:
    """Promote detected CJK headings to ATX so the shared IR builder can use them."""
    if re.search(r"^#{1,6}\s+\S", text, flags=re.MULTILINE):
        return text  # already markdown, leave it alone

    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip().strip("　").strip()
        if _looks_like_heading(line):
            out.append(f"## {stripped}")
        else:
            out.append(stripped)
    return "\n".join(out)


class TextParser:
    name = "text"
    version = VERSION
    suffixes = {".txt", ".md", ".markdown", ".text", ".rst", ".log", ".csv"}
    mimes = {"text/plain", "text/markdown", "text/csv", "application/markdown"}

    def supports(self, mime: str, suffix: str) -> bool:
        return suffix.lower() in self.suffixes or mime.lower() in self.mimes

    def parse(self, data: bytes, *, filename: str, mime: str) -> ParsedDocument:
        raw = normalize_text(decode_bytes(data))
        title = _title_from(filename, raw)
        document = build_from_markdown(
            to_markdown(raw), parser=self.name, version=self.version, title=title
        )
        document.meta = DocumentMeta(
            title=title,
            lang=_guess_lang(raw),
            parser=self.name,
            parser_version=self.version,
            extra={"source_mime": mime},
        )
        return document


def _title_from(filename: str, text: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in (".txt", ".md", ".markdown", ".text"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if stem:
        return stem
    first = next((line.strip() for line in text.split("\n") if line.strip()), "")
    return first[:120]


def _guess_lang(text: str) -> str:
    sample = text[:2000]
    if not sample:
        return "unknown"
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    return "zh" if cjk / len(sample) > 0.15 else "en"

"""Language detection for plagiarism corpus projections.

pysbd needs a language to segment sentences. Detection runs once per projection
and the result is stored on the projection row.

Library: `langdetect` (a port of Google's language-detection). Pure-Python,
dependency-light, returns ISO 639-1 codes that map onto pysbd once the region
subtag is dropped, and - with a fixed seed - deterministic, which the rest of
the pipeline relies on.

Ported from noplag-engine `src/noplag_engine/ingestion/language.py` at commit
005da60faad21bf52702997d73583b78d8905d22, Apache-2.0 (see
`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`).
Modified for kbsvc: only the language-inference half is ported; upstream's
extract/normalize/storage helpers live in the same package there and are not
used here. Detection semantics unchanged.
"""

from __future__ import annotations

from langdetect import DetectorFactory, LangDetectException, detect

# langdetect's inference is randomized; pin the seed so the same text always
# yields the same language. The whole pipeline is deterministic by design and
# a check must be reproducible from its snapshot.
DetectorFactory.seed = 0

# ISO 639-1 codes pysbd can segment. A detected language outside this set still
# gets recorded on the projection, but chunking falls back to English rules -
# acceptable for fingerprinting, since k-grams are character-level.
_PYSBD_SUPPORTED = frozenset(
    {
        "en", "es", "de", "fr", "it", "pt", "ru", "ja", "zh", "nl",
        "pl", "da", "el", "ar", "fa", "hi", "mr", "my", "ur", "bg",
        "am", "hy", "kk", "sk",
    }
)  # fmt: skip

_FALLBACK = "en"


def detect_language(text: str) -> str:
    """Return the detected ISO 639-1 code for `text`, or "en" on failure.

    Empty or feature-poor text falls back to English rather than raising - a
    projection always gets *some* language recorded.
    """
    if not text or not text.strip():
        return _FALLBACK
    try:
        return detect(text)
    except LangDetectException:
        return _FALLBACK


def has_cjk(text: str) -> bool:
    """True when *text* contains at least one CJK Unified Ideograph character."""
    return any("㐀" <= char <= "鿿" for char in text)


def is_chinese_dominant(text: str, *, threshold: float = 0.5) -> bool:
    """True when at least *threshold* of non-space chars are CJK ideographs."""
    meaningful = [char for char in text if not char.isspace()]
    if not meaningful:
        return False
    han = sum("㐀" <= char <= "鿿" for char in meaningful)
    return han / len(meaningful) >= threshold


def pysbd_language(code: str) -> str:
    """Map a detected language code to one pysbd can segment.

    The region subtag is dropped before the lookup: langdetect returns a bare
    ISO 639-1 code for every language it knows except Chinese, where it reports
    `zh-cn` / `zh-tw`. Matching the full string means neither ever reaches the
    `zh` entry.

    This matters more here than upstream - this corpus is largely classical
    Chinese, so the `zh` path is the common case, not an edge case.
    """
    base = code.split("-", 1)[0]
    return base if base in _PYSBD_SUPPORTED else _FALLBACK

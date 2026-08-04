"""Citation assembly: turn a raw chunk into something a human can verify."""

from __future__ import annotations

from ..embedding.sparse_bm25 import tokenize


def build_snippet(text: str, query: str, *, width: int) -> tuple[str, list[list[int]]]:
    """Window the chunk around the densest match, returning offsets into the snippet."""
    if not text:
        return "", []
    terms = {term for term in tokenize(query) if len(term) > 1} or set(tokenize(query))
    if not terms:
        return text[:width], []

    center = _best_window_start(text, terms, width)
    start = max(center, 0)
    end = min(start + width, len(text))
    snippet = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""

    highlights = _find_spans(snippet, terms, offset=len(prefix))
    return f"{prefix}{snippet}{suffix}", highlights


def _best_window_start(text: str, terms: set[str], width: int) -> int:
    if len(text) <= width:
        return 0
    step = max(width // 4, 1)
    best_start, best_hits = 0, -1
    for start in range(0, max(len(text) - width, 0) + 1, step):
        window = text[start : start + width]
        hits = sum(window.count(term) for term in terms)
        if hits > best_hits:
            best_start, best_hits = start, hits
    return best_start


def _find_spans(snippet: str, terms: set[str], *, offset: int) -> list[list[int]]:
    spans: list[list[int]] = []
    for term in sorted(terms, key=len, reverse=True):
        start = snippet.find(term)
        while start != -1 and len(spans) < 32:
            span = [start + offset, start + offset + len(term)]
            if not any(s[0] <= span[0] < s[1] for s in spans):
                spans.append(span)
            start = snippet.find(term, start + 1)
    return sorted(spans)


def source_anchor(source_uri: str, page: int | None) -> str:
    """Append a page anchor so the citation opens where the text actually is."""
    if not source_uri or not page:
        return source_uri
    separator = "&" if "#" in source_uri else "#"
    return f"{source_uri}{separator}page={page}"

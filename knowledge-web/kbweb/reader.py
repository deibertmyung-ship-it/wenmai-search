"""Pure reader presentation helpers for safely rendering hit ranges."""

from __future__ import annotations


def with_highlight_fragments(chunk: dict) -> dict:
    """Return a copy with escaped-by-template text fragments and local marks.

    The helper only returns strings and booleans. Jinja remains responsible for
    escaping every string; no pre-built HTML is ever returned.
    """

    text = str(chunk.get("text") or "")
    ranges = []
    for item in chunk.get("highlights") or []:
        try:
            start = int(item["local_start"])
            end = int(item["local_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start < end <= len(text):
            ranges.append((start, end))
    ranges.sort()

    fragments: list[dict[str, object]] = []
    cursor = 0
    anchor_pending = True
    for start, end in ranges:
        if start < cursor:
            start = cursor
        if start > cursor:
            fragments.append({"text": text[cursor:start], "highlighted": False, "anchor": False})
        if end <= start:
            continue
        fragments.append(
            {"text": text[start:end], "highlighted": True, "anchor": anchor_pending}
        )
        anchor_pending = False
        cursor = end
    if cursor < len(text):
        fragments.append({"text": text[cursor:], "highlighted": False, "anchor": False})
    if not fragments and text:
        fragments.append({"text": text, "highlighted": False, "anchor": False})

    rendered = dict(chunk)
    rendered["fragments"] = fragments
    return rendered


def decorate_chunks(chunks: list[dict]) -> list[dict]:
    return [with_highlight_fragments(chunk) for chunk in chunks]

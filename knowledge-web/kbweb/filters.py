"""Jinja filters and helpers.

The important one is `highlight_segments`: kbsvc returns character offsets, and
we slice on those rather than re-matching strings in the template. Re-matching
would highlight text the backend never scored, which is a lie about provenance.
"""

from __future__ import annotations

from datetime import datetime

_JOB_TONE = {
    "completed": "ok",
    "failed": "fail",
    "cancelled": "fail",
    "pending": "",
    "retry_wait": "running",
    "parsing": "running",
    "chunking": "running",
    "embedding": "running",
    "indexing": "running",
}


def highlight_segments(text: str, spans: list[list[int]] | None) -> list[tuple[str, bool]]:
    """Split text into (fragment, is_hit) pairs using backend-supplied offsets.

    Overlapping or out-of-range spans are dropped rather than trusted, so a bad
    payload degrades to plain text instead of scrambling the passage.
    """
    if not text:
        return []
    valid = sorted(
        (start, end)
        for start, end in (tuple(span[:2]) for span in spans or [] if len(span) >= 2)
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)
    )
    if not valid:
        return [(text, False)]

    segments: list[tuple[str, bool]] = []
    cursor = 0
    for start, end in valid:
        if start < cursor:
            continue  # overlaps the previous hit; skip rather than double-mark
        if start > cursor:
            segments.append((text[cursor:start], False))
        segments.append((text[start:end], True))
        cursor = end
    if cursor < len(text):
        segments.append((text[cursor:], False))
    return segments


def breadcrumb(heading_path: list[str] | None) -> str:
    return " › ".join(heading_path or []) or "—"


def job_tone(state: str) -> str:
    return _JOB_TONE.get(state, "")


def short_id(value: str, keep: int = 8) -> str:
    return value[:keep] if value else "—"


def ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}ms" if value >= 1 else f"{value:.2f}ms"


def score(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def timeago(value: str | None) -> str:
    """Coarse relative time; the exact timestamp stays in the title attribute."""
    if not value:
        return "—"
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    delta = (datetime.utcnow() - stamp).total_seconds()
    if delta < 60:
        return "刚刚"
    for limit, divisor, unit in ((3600, 60, "分钟"), (86400, 3600, "小时"), (2592000, 86400, "天")):
        if delta < limit:
            return f"{int(delta // divisor)}{unit}前"
    return stamp.strftime("%Y-%m-%d")


def filesize(value: int | None) -> str:
    if not value:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def register(app) -> None:
    app.jinja_env.filters.update(
        {
            "breadcrumb": breadcrumb,
            "job_tone": job_tone,
            "short_id": short_id,
            "ms": ms,
            "score": score,
            "timeago": timeago,
            "filesize": filesize,
        }
    )
    app.jinja_env.globals["highlight_segments"] = highlight_segments

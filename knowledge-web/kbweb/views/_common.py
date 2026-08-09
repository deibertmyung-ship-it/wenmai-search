"""Shared view helpers."""

from __future__ import annotations

from flask import current_app

from ..client import KbClient
from ..config import Config

TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})


def client() -> KbClient:
    return current_app.extensions["kb_client"]()


def settings() -> Config:
    return current_app.config["KBWEB"]


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def as_int(value: str | None, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def job_context(api: KbClient, *, state: str = "", limit: int = 100) -> dict:
    """Return the shared task table context used by import and legacy jobs pages."""
    def list_all(*, job_state: str | None = None) -> list[dict]:
        rows: list[dict] = []
        offset = 0
        while True:
            page = api.list_jobs(state=job_state, limit=limit, offset=offset)
            rows.extend(page)
            if len(page) < limit:
                return rows
            offset += len(page)

    jobs = list_all(job_state=state or None)
    all_jobs = jobs if not state else list_all()
    counts: dict[str, int] = {}
    for job in all_jobs:
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    return {
        "jobs": jobs,
        "counts": counts,
        "total_count": len(all_jobs),
        "active": state,
        "live": any(job["state"] not in TERMINAL_JOB_STATES for job in jobs),
    }

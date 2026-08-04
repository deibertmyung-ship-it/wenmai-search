"""Shared view helpers."""

from __future__ import annotations

from flask import current_app

from ..client import KbClient
from ..config import Config


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

"""Configuration, driven by KBWEB_* environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    api_base: str = field(
        default_factory=lambda: os.environ.get("KBWEB_API_BASE", "http://127.0.0.1:8077").rstrip("/")
    )
    api_key: str = field(default_factory=lambda: os.environ.get("KBWEB_API_KEY", ""))
    timeout: float = field(default_factory=lambda: float(os.environ.get("KBWEB_TIMEOUT", "30")))
    page_size: int = field(default_factory=lambda: int(os.environ.get("KBWEB_PAGE_SIZE", "10")))
    reader_page_size: int = field(
        default_factory=lambda: int(os.environ.get("KBWEB_READER_PAGE_SIZE", "12"))
    )
    debug_ui: bool = field(default_factory=lambda: _flag("KBWEB_DEBUG_UI", True))
    # Ship no <script> at all. Every page is server-rendered and every control
    # is a form or a link, so this is a supported way to run kbweb - not a
    # degraded one. Making it a config flag rather than a browser setting is
    # what lets the unit suite assert the no-JS contract on every route.
    nojs: bool = field(default_factory=lambda: _flag("KBWEB_NOJS", False))
    secret_key: str = field(
        default_factory=lambda: os.environ.get("KBWEB_SECRET_KEY") or secrets.token_hex(32)
    )
    max_content_length: int = field(
        default_factory=lambda: int(os.environ.get("KBWEB_MAX_UPLOAD_BYTES", MAX_UPLOAD_BYTES))
    )

    def as_flask_mapping(self) -> dict:
        return {
            "SECRET_KEY": self.secret_key,
            "MAX_CONTENT_LENGTH": self.max_content_length,
            "JSON_AS_ASCII": False,
            "TEMPLATES_AUTO_RELOAD": _flag("KBWEB_TEMPLATE_RELOAD", False),
        }

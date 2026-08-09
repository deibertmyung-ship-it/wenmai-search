"""CSRF protection for kbweb.

Dependency-free token based on the Flask session and ``SECRET_KEY``.  Every
POST/PUT/DELETE form must include a hidden ``_csrf_token`` field; AJAX calls
may send it as the ``X-CSRF-Token`` header instead.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from flask import Flask, current_app, request, session


def _generate_token() -> str:
    """A token tied to a per-session nonce and the app SECRET_KEY."""
    sid = session.get("_csrf_sid")
    if not sid:
        sid = secrets.token_hex(16)
        session["_csrf_sid"] = sid
    key = current_app.config["SECRET_KEY"]
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, sid.encode(), hashlib.sha256).hexdigest()


def _validate_token(token: str | None) -> bool:
    if not token:
        return False
    expected = _generate_token()
    return hmac.compare_digest(token, expected)


def init_csrf(app: Flask) -> None:
    """Register the before-request guard and the ``csrf_token`` template global."""

    @app.before_request
    def _check_csrf():  # type: ignore[no-untyped-def]
        if current_app.testing:
            return
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        if not _validate_token(token):
            return "CSRF token missing or invalid", 400

    app.jinja_env.globals["csrf_token"] = _generate_token

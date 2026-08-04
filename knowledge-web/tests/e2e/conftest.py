"""End-to-end fixtures.

A real browser drives a real Flask process. The kbsvc backend is replaced by a
deterministic stub so these tests assert on *frontend behaviour*, not on corpus
contents — the backend already has its own suite.

Chrome is used via `channel="chrome"` rather than Playwright's bundled Chromium:
this machine throttles large downloads, and testing against the browser people
actually use is not a downgrade.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

pytest.importorskip("playwright", reason="playwright not installed")

from kbweb import create_app  # noqa: E402
from kbweb.config import Config  # noqa: E402

from .stub_backend import FakeKbClient  # noqa: E402

BROWSER_CHANNEL = os.environ.get("KBWEB_E2E_CHANNEL", "chrome")
HEADLESS = os.environ.get("KBWEB_E2E_HEADED", "") == ""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):  # noqa: A002 - silence per-request logging
        pass


@pytest.fixture(scope="session")
def live_server():
    """Serve kbweb on a real port with the backend stubbed out."""
    from unittest.mock import patch

    port = _free_port()
    with patch("kbweb.KbClient", FakeKbClient):
        app = create_app(Config(api_base="http://stub", secret_key="e2e", debug_ui=True))
        server = make_server("127.0.0.1", port, app, handler_class=_QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base = f"http://127.0.0.1:{port}"
        _wait_until_up(base)
        try:
            yield base
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _wait_until_up(base: str, timeout: float = 10.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base, timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"live server did not start at {base}")


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    return {**browser_type_launch_args, "channel": BROWSER_CHANNEL, "headless": HEADLESS}


@pytest.fixture
def page(page):
    """Fail loudly on console errors and unhandled page exceptions."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
        if msg.type == "error"
        else None,
    )
    yield page
    assert not errors, "browser reported errors:\n" + "\n".join(errors)

"""Flask application factory for kbweb."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, g, render_template

from . import filters
from .client import KbClient
from .config import Config
from .errors import BackendError, BackendUnavailable

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    app.config["KBWEB"] = config or Config()
    app.config.update(app.config["KBWEB"].as_flask_mapping())

    filters.register(app)
    _register_client(app)
    _register_errors(app)
    _register_blueprints(app)

    # Resolved once at startup, not per request: deploy/build-fonts.py either
    # ran before this process booted or it did not.
    has_webfonts = _webfonts_present(app)

    @app.context_processor
    def _inject_globals() -> dict:
        settings: Config = app.config["KBWEB"]
        return {
            "kbweb_version": __version__,
            "debug_ui_enabled": settings.debug_ui,
            "nojs": settings.nojs,
            "webfonts": has_webfonts,
        }

    return app


def _webfonts_present(app: Flask) -> bool:
    """True when deploy/build-fonts.py has produced at least one face.

    Checked against a shard manifest rather than against fonts.css, so a
    stylesheet left behind by an earlier build cannot make the template link
    imports whose targets were since removed.
    """
    static_folder = app.static_folder
    if static_folder is None:  # pragma: no cover - Flask always sets this
        return False
    fonts = Path(static_folder) / "fonts"
    return (Path(static_folder) / "css" / "fonts.css").exists() and any(
        fonts.glob("*/result.css")
    )


def _register_client(app: Flask) -> None:
    """One client per request context; the config object is shared."""

    def get_client() -> KbClient:
        if "kb_client" not in g:
            g.kb_client = KbClient(app.config["KBWEB"])
        return g.kb_client

    @app.teardown_appcontext
    def _close_client(_exception: BaseException | None) -> None:
        client = g.pop("kb_client", None)
        if client is not None:
            client.close()

    app.extensions["kb_client"] = get_client


def _register_errors(app: Flask) -> None:
    @app.errorhandler(BackendUnavailable)
    def _backend_down(exc: BackendUnavailable):
        logger.warning("backend unavailable: %s", exc.reason)
        # The base URL is deliberately not rendered - it can name an internal host.
        return render_template("errors/backend_down.html", reason=exc.reason), 503

    @app.errorhandler(BackendError)
    def _backend_error(exc: BackendError):
        status = exc.status if 400 <= exc.status < 500 else 502
        return render_template("errors/backend_error.html", error=exc), status

    @app.errorhandler(404)
    def _not_found(_exc):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def _too_large(_exc):
        limit = app.config["MAX_CONTENT_LENGTH"]
        return render_template("errors/413.html", limit=limit), 413


def _register_blueprints(app: Flask) -> None:
    from .views import api, ingest, jobs, library, search

    app.register_blueprint(search.bp)
    app.register_blueprint(library.bp)
    app.register_blueprint(ingest.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(api.bp)

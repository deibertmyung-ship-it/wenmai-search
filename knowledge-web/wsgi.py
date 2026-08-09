"""WSGI entrypoint.

    flask --app wsgi run --port 5055          # development
    waitress-serve --port 5055 wsgi:app       # production
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Explicit path: under systemd/waitress the cwd may not be the project root,
# which would silently skip .env and leave the app misconfigured.
load_dotenv(Path(__file__).resolve().parent / ".env")

from kbweb import create_app  # noqa: E402  (after load_dotenv, by design)

app = create_app()

if __name__ == "__main__":  # pragma: no cover
    app.run(host="127.0.0.1", port=5055, debug=True)

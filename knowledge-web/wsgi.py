"""WSGI entrypoint.

    flask --app wsgi run --port 5055          # development
    waitress-serve --port 5055 wsgi:app       # production
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from kbweb import create_app  # noqa: E402  (after load_dotenv, by design)

app = create_app()

if __name__ == "__main__":  # pragma: no cover
    app.run(host="127.0.0.1", port=5055, debug=True)

"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db.session import init_db
from ..errors import KbError
from .routers import admin, documents, ingest, search, sources

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="kbsvc",
        version="0.1.0",
        description="Self-hosted knowledge retrieval: ingest, hybrid search, citations.",
        lifespan=lifespan,
    )

    @app.exception_handler(KbError)
    async def _kb_error_handler(_: Request, exc: KbError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_envelope())

    @app.exception_handler(Exception)
    async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        envelope = {
            "error": {"code": "internal_error", "message": "internal error", "detail": {}}
        }
        return JSONResponse(status_code=500, content=envelope)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "profile": settings.profile}

    @app.get("/readyz", tags=["ops"])
    def readyz() -> dict:
        from sqlalchemy import text

        from ..db.session import get_engine

        checks: dict[str, str] = {}
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {exc}"
        return {"status": "ok" if all(v == "ok" for v in checks.values()) else "degraded", **checks}

    for router in (search.router, sources.router, documents.router, ingest.router, admin.router):
        app.include_router(router, prefix="/v1")

    return app


app = create_app()

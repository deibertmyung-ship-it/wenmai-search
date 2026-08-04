"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from threading import Event, Thread

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import get_settings
from ..db.session import init_db
from ..embedding import get_dense_embedder
from ..errors import KbError
from ..ingest.worker import IngestWorker
from ..vector import get_vector_store, reset_vector_store
from .routers import admin, documents, ingest, search, sources

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    stop_event: Event | None = None
    worker_thread: Thread | None = None

    app.state.ingest_worker_thread = None
    if settings.run_api_worker:
        # Initialize expensive process-wide singletons before request and worker
        # threads can race to create separate embedded Qdrant/model instances.
        embedder = get_dense_embedder()
        get_vector_store().ensure_collection(embedder.dim)

        stop_event = Event()
        worker = IngestWorker(settings=settings, owner="api-embedded-worker")

        def run_worker() -> None:
            try:
                worker.run_forever(stop_event=stop_event)
            except Exception:
                logger.exception("API-embedded ingest worker stopped unexpectedly")

        worker_thread = Thread(
            target=run_worker,
            name="kbsvc-ingest-worker",
            daemon=True,
        )
        app.state.ingest_worker_thread = worker_thread
        worker_thread.start()
        logger.info("started API-embedded ingest worker")

    try:
        yield
    finally:
        if stop_event is not None and worker_thread is not None:
            stop_event.set()
            await asyncio.to_thread(worker_thread.join, settings.api_worker_shutdown_timeout)
            if worker_thread.is_alive():
                logger.warning(
                    "ingest worker did not stop within %.1fs; current lease will be reclaimed",
                    settings.api_worker_shutdown_timeout,
                )
            else:
                logger.info("stopped API-embedded ingest worker")
                reset_vector_store()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Wenmai Search API",
        version="0.1.0",
        description="Self-hosted, traceable hybrid knowledge retrieval via REST and MCP.",
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
        thread = getattr(app.state, "ingest_worker_thread", None)
        worker = "running" if thread is not None and thread.is_alive() else "disabled"
        if settings.run_api_worker and thread is not None and not thread.is_alive():
            worker = "stopped"
        return {"status": "ok", "profile": settings.profile, "worker": worker}

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
        if settings.run_api_worker:
            thread = getattr(app.state, "ingest_worker_thread", None)
            checks["worker"] = "ok" if thread is not None and thread.is_alive() else "error"
        return {"status": "ok" if all(v == "ok" for v in checks.values()) else "degraded", **checks}

    for router in (search.router, sources.router, documents.router, ingest.router, admin.router):
        app.include_router(router, prefix="/v1")

    return app


app = create_app()

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
from ..lexical import get_lexical_store, reset_lexical_store
from ..ratelimit import SlidingWindowLimiter
from ..vector import get_vector_store, reset_vector_store
from .routers import admin, documents, ingest, plagiarism, search, sources

logger = logging.getLogger(__name__)

_limiter: SlidingWindowLimiter | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    stop_event: Event | None = None
    worker_thread: Thread | None = None

    app.state.ingest_worker_thread = None
    if settings.run_api_worker:
        # Initialize process-wide singletons before request and worker threads
        # race to open the stores and the embedder.
        embedder = get_dense_embedder()
        get_vector_store().ensure_collection(embedder.dim)
        get_lexical_store().ensure_ready()

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
                reset_lexical_store()


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

    # Rate limiting (opt-in via KB_RATE_LIMIT_PER_MINUTE).
    global _limiter
    if settings.rate_limit_per_minute > 0:
        _limiter = SlidingWindowLimiter(
            max_requests=settings.rate_limit_per_minute, window_seconds=60.0
        )

        @app.middleware("http")
        async def _rate_limit_middleware(request: Request, call_next):
            client_ip = request.client.host if request.client else "unknown"
            if not _limiter or not _limiter.allow(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "rate_limited",
                            "message": "too many requests",
                            "detail": {},
                        }
                    },
                )
            return await call_next(request)

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

        # Only when the feature is actually serving traffic. Reporting degraded
        # for an unbuilt corpus on an install that does not offer plagiarism
        # would make the probe useless everywhere else.
        if settings.plag_enabled:
            checks.update(_plagiarism_readiness(settings))

        return {"status": "ok" if all(v == "ok" for v in checks.values()) else "degraded", **checks}

    # Registered unconditionally. When the feature is off or the store is not
    # PostgreSQL the routes answer 503 - a route that disappears entirely makes
    # "not enabled" indistinguishable from "wrong URL".
    for router in (
        search.router,
        sources.router,
        documents.router,
        ingest.router,
        admin.router,
        plagiarism.router,
    ):
        app.include_router(router, prefix="/v1")

    return app


def _plagiarism_readiness(settings) -> dict[str, str]:
    """Schema, worker liveness and corpus coverage, for `/readyz`.

    Each answer is `ok` or a short reason. Every failure mode here is one that
    lets checks be *accepted* while producing meaningless results - a missing
    GIN index, no live worker, or a corpus that has not finished building - so
    the probe has to look past "the database answers".
    """
    from ..plagiarism.corpus_ops import corpus_report

    try:
        report = corpus_report(settings.default_tenant)
    except Exception as exc:  # noqa: BLE001 - readiness must not raise
        return {"plagiarism": f"error: {type(exc).__name__}: {exc}"}

    checks = {}
    schema = report["schema"]
    if schema["ready"]:
        checks["plagiarism_schema"] = "ok"
    elif schema["missing_tables"]:
        checks["plagiarism_schema"] = f"missing tables: {', '.join(schema['missing_tables'])}"
    elif schema.get("missing_columns"):
        checks["plagiarism_schema"] = f"missing columns: {', '.join(schema['missing_columns'])}"
    else:
        checks["plagiarism_schema"] = "gin index missing"

    checks["plagiarism_worker"] = "ok" if report["live_workers"] else "no live worker"

    coverage = report["coverage"]
    if coverage["total"] == 0 or coverage["ready"] == coverage["total"]:
        checks["plagiarism_corpus"] = "ok"
    else:
        checks["plagiarism_corpus"] = (
            f"{coverage['ready']}/{coverage['total']} ready, "
            f"{coverage['pending']} pending, {coverage['failed']} failed"
        )
    return checks


app = create_app()

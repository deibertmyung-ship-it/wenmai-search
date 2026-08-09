"""Library: sources, documents, and the reader."""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, redirect, render_template, request, url_for

from ..errors import BackendError, BackendUnavailable
from ..reader import decorate_chunks
from ._common import as_int, client, settings

logger = logging.getLogger(__name__)
bp = Blueprint("library", __name__, url_prefix="")

# How many chunks of run-up to show before a focused hit, so it lands in context
# rather than flush against the top of the page.
READER_LEAD_IN = 2


@bp.get("/library")
def shelf():
    query = (request.args.get("q") or "").strip()
    source_id = (request.args.get("source_id") or "").strip()
    document_id = (request.args.get("document_id") or "").strip()
    api = client()

    sources = api.list_sources()
    filter_documents = api.list_documents(source_id=source_id or None, limit=500)
    documents = (
        [document for document in filter_documents if document["id"] == document_id]
        if document_id
        else (
            filter_documents
            if not query
            else api.list_documents(source_id=source_id or None, q=query, limit=500)
        )
    )

    by_source: dict[str, list[dict]] = {}
    for document in documents:
        by_source.setdefault(document["source_id"], []).append(document)

    groups = [
        {"source": source, "documents": by_source.get(source["id"], [])}
        for source in sources
        if by_source.get(source["id"]) or not (query or source_id)
    ]
    filter_by_source: dict[str, list[dict]] = {}
    for document in filter_documents:
        filter_by_source.setdefault(document["source_id"], []).append(document)
    filter_groups = [
        {"source": source, "documents": filter_by_source.get(source["id"], [])}
        for source in sources
        if filter_by_source.get(source["id"])
    ]
    try:
        stats = api.stats()
    except (BackendError, BackendUnavailable):
        logger.warning("stats unavailable for library shelf", exc_info=True)
        stats = None

    return render_template(
        "library.html",
        groups=groups,
        filter_groups=filter_groups,
        sources=sources,
        stats=stats,
        form={"q": query, "source_id": source_id, "document_id": document_id},
    )


@bp.get("/library/<document_id>")
def document(document_id: str):
    api = client()
    doc = api.get_document(document_id)
    preview = api.get_chunks(document_id, from_ordinal=0, limit=3)
    return render_template(
        "document.html",
        document=doc,
        preview=preview,
        plagiarism_form_token=uuid.uuid4().hex,
    )


@bp.get("/read/<document_id>")
def read(document_id: str):
    config = settings()
    api = client()
    start = as_int(request.args.get("from"), 0, low=0, high=1_000_000)
    focus = request.args.get("focus")
    version_raw = request.args.get("version")
    version = int(version_raw) if version_raw and version_raw.isdigit() else None
    hit_start_raw = request.args.get("hit_start")
    hit_end_raw = request.args.get("hit_end")
    hit_mode = hit_start_raw is not None or hit_end_raw is not None
    hit_start = int(hit_start_raw) if hit_start_raw and hit_start_raw.isdigit() else None
    hit_end = int(hit_end_raw) if hit_end_raw and hit_end_raw.isdigit() else None

    # A search hit deep in a book links here with ?focus=N. Opening at ordinal 0
    # would strand the reader at the top with nothing highlighted, so centre the
    # window on the hit unless an explicit ?from= overrides it.
    focus_ordinal = int(focus) if (focus or "").isdigit() else None
    if focus_ordinal is not None and "from" not in request.args:
        start = max(focus_ordinal - READER_LEAD_IN, 0)

    doc = api.get_document(document_id)
    reader_error = None
    focus_ordinal = int(focus) if (focus or "").isdigit() else None
    if hit_mode and (version is None or hit_start is None or hit_end is None):
        reader_error = "链接中的历史版本或命中坐标无效，无法精确定位。"
        chunks = []
        has_more = False
        next_from = 0
    elif hit_mode:
        try:
            window = api.get_passage_window(
                document_id,
                version=version,
                start=hit_start,
                end=hit_end,
                context=READER_LEAD_IN,
            )
            chunks = window.get("chunks") or []
            chunks = decorate_chunks(chunks)
            start = int(window.get("from_ordinal", start))
            next_from = int(window.get("next_from", start + len(chunks)))
            has_more = bool(window.get("has_more"))
            focus_ordinal = int(window["focus_ordinal"])
        except BackendError as exc:
            if exc.code not in {"passage_location_unavailable", "version_not_found"}:
                raise
            reader_error = "该检测版本的正文已无法精确定位，您仍可打开该版本的普通阅读内容。"
            chunks = api.get_chunks(
                document_id, from_ordinal=0, limit=config.reader_page_size, version=version
            )
            start = 0
            next_from = config.reader_page_size
            has_more = len(chunks) == config.reader_page_size
    else:
        chunks = api.get_chunks(
            document_id,
            from_ordinal=start,
            limit=config.reader_page_size,
            version=version,
        )
        next_from = start + config.reader_page_size
        has_more = len(chunks) == config.reader_page_size

    return render_template(
        "reader.html",
        document=doc,
        chunks=chunks,
        start=start,
        next_from=next_from,
        has_more=has_more,
        focus=focus_ordinal,
        page_size=config.reader_page_size,
        reader_version=version,
        hit_mode=hit_mode,
        reader_error=reader_error,
    )


@bp.post("/library/<document_id>/reindex")
def reindex(document_id: str):
    client().reindex_document(document_id)
    return redirect(url_for("jobs.index"))

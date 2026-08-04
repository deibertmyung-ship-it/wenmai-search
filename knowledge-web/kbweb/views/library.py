"""Library: sources, documents, and the reader."""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from ._common import as_int, client, settings

bp = Blueprint("library", __name__, url_prefix="")

# How many chunks of run-up to show before a focused hit, so it lands in context
# rather than flush against the top of the page.
READER_LEAD_IN = 2


@bp.get("/library")
def shelf():
    query = (request.args.get("q") or "").strip()
    source_id = (request.args.get("source_id") or "").strip()
    api = client()

    sources = api.list_sources()
    documents = api.list_documents(source_id=source_id or None, q=query or None, limit=500)

    by_source: dict[str, list[dict]] = {}
    for document in documents:
        by_source.setdefault(document["source_id"], []).append(document)

    groups = [
        {"source": source, "documents": by_source.get(source["id"], [])}
        for source in sources
        if by_source.get(source["id"]) or not (query or source_id)
    ]
    try:
        stats = api.stats()
    except Exception:  # stats are decoration here, never a reason to fail the page
        stats = None

    return render_template(
        "library.html",
        groups=groups,
        sources=sources,
        stats=stats,
        form={"q": query, "source_id": source_id},
    )


@bp.get("/library/<document_id>")
def document(document_id: str):
    api = client()
    doc = api.get_document(document_id)
    preview = api.get_chunks(document_id, from_ordinal=0, limit=3)
    return render_template("document.html", document=doc, preview=preview)


@bp.get("/read/<document_id>")
def read(document_id: str):
    config = settings()
    api = client()
    start = as_int(request.args.get("from"), 0, low=0, high=1_000_000)
    focus = request.args.get("focus")

    # A search hit deep in a book links here with ?focus=N. Opening at ordinal 0
    # would strand the reader at the top with nothing highlighted, so centre the
    # window on the hit unless an explicit ?from= overrides it.
    focus_ordinal = int(focus) if (focus or "").isdigit() else None
    if focus_ordinal is not None and "from" not in request.args:
        start = max(focus_ordinal - READER_LEAD_IN, 0)

    doc = api.get_document(document_id)
    chunks = api.get_chunks(document_id, from_ordinal=start, limit=config.reader_page_size)
    has_more = len(chunks) == config.reader_page_size

    return render_template(
        "reader.html",
        document=doc,
        chunks=chunks,
        start=start,
        next_from=start + config.reader_page_size,
        has_more=has_more,
        focus=focus_ordinal,
        page_size=config.reader_page_size,
    )


@bp.post("/library/<document_id>/reindex")
def reindex(document_id: str):
    client().reindex_document(document_id)
    return redirect(url_for("jobs.index"))

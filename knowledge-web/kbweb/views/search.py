"""Search view - the front door."""

from __future__ import annotations

from flask import Blueprint, render_template, request

from ..errors import BackendUnavailable
from ._common import as_bool, as_int, client, settings

bp = Blueprint("search", __name__)

MODES = ("hybrid", "dense", "sparse")


@bp.get("/")
def index():
    config = settings()
    query = (request.args.get("q") or "").strip()
    mode = request.args.get("mode", "hybrid")
    if mode not in MODES:
        mode = "hybrid"
    top_k = as_int(request.args.get("top_k"), config.page_size, low=1, high=50)
    source_id = (request.args.get("source_id") or "").strip()
    heading = (request.args.get("heading") or "").strip()
    # An unchecked checkbox is simply absent from the query string, which is
    # indistinguishable from "the form was never submitted". The hidden `f`
    # marker disambiguates: once it is present, absence means the user turned
    # the option off, so the default must flip to False.
    submitted = request.args.get("f") == "1"
    checkbox_default = not submitted
    rerank = as_bool(request.args.get("rerank"), checkbox_default)
    rewrite = as_bool(request.args.get("rewrite"), checkbox_default)
    want_debug = config.debug_ui and as_bool(request.args.get("debug"), False)

    form = {
        "q": query,
        "mode": mode,
        "top_k": top_k,
        "source_id": source_id,
        "heading": heading,
        "rerank": rerank,
        "rewrite": rewrite,
        "debug": want_debug,
    }

    # Sources populate the filter dropdown; a missing backend must not break the
    # page before the user has even searched.
    try:
        sources = client().list_sources()
    except BackendUnavailable:
        if not query:
            raise
        sources = []

    if not query:
        return render_template("search.html", form=form, sources=sources, payload=None)

    payload = client().search(
        query,
        top_k=top_k,
        mode=mode,
        source_ids=[source_id] if source_id else None,
        heading_contains=heading or None,
        rerank=rerank,
        rewrite=rewrite,
        debug=want_debug,
    )
    return render_template("search.html", form=form, sources=sources, payload=payload)

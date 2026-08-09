"""Small JSON endpoints for progressive enhancement.

These exist so the browser never talks to kbsvc directly - the API key stays
server-side and the same auth applies to every call.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ._common import as_int, client, job_context, settings

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.get("/chunks/<document_id>")
def chunks(document_id: str):
    config = settings()
    start = as_int(request.args.get("from"), 0, low=0, high=1_000_000)
    limit = as_int(request.args.get("limit"), config.reader_page_size, low=1, high=50)
    version_raw = request.args.get("version")
    version = int(version_raw) if version_raw and version_raw.isdigit() else None
    rows = client().get_chunks(
        document_id, from_ordinal=start, limit=limit, version=version
    )
    return jsonify({"chunks": rows, "next_from": start + limit, "has_more": len(rows) == limit})


@bp.get("/jobs")
def jobs():
    state = (request.args.get("state") or "").strip()
    context = job_context(client(), state=state, limit=200)
    return jsonify({"jobs": context["jobs"], "counts": context["counts"]})

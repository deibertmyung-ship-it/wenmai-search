"""Small JSON endpoints for progressive enhancement.

These exist so the browser never talks to kbsvc directly - the API key stays
server-side and the same auth applies to every call.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ._common import as_int, client, settings

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.get("/chunks/<document_id>")
def chunks(document_id: str):
    config = settings()
    start = as_int(request.args.get("from"), 0, low=0, high=1_000_000)
    limit = as_int(request.args.get("limit"), config.reader_page_size, low=1, high=50)
    rows = client().get_chunks(document_id, from_ordinal=start, limit=limit)
    return jsonify({"chunks": rows, "next_from": start + limit, "has_more": len(rows) == limit})


@bp.get("/jobs")
def jobs():
    state = (request.args.get("state") or "").strip()
    rows = client().list_jobs(state=state or None, limit=200)
    counts: dict[str, int] = {}
    for job in rows:
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    return jsonify({"jobs": rows, "counts": counts})

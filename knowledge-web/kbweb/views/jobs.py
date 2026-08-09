"""Job monitoring and retry."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..errors import BackendError
from ._common import as_int, client

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

TERMINAL = {"completed", "failed", "cancelled"}


@bp.get("/")
def index():
    state = (request.args.get("state") or "").strip()
    limit = as_int(request.args.get("limit"), 100, low=1, high=500)
    api = client()

    jobs = api.list_jobs(state=state or None, limit=limit)
    # The rail is persistent navigation, so its state links and counts must
    # remain global even while the table itself is filtered.
    all_jobs = jobs if not state else api.list_jobs(limit=limit)
    counts: dict[str, int] = {}
    for job in all_jobs:
        counts[job["state"]] = counts.get(job["state"], 0) + 1

    return render_template(
        "jobs.html",
        jobs=jobs,
        counts=counts,
        total_count=len(all_jobs),
        active=state,
        # Poll only while something can still change.
        live=any(job["state"] not in TERMINAL for job in jobs),
    )


@bp.post("/<job_id>/retry")
def retry(job_id: str):
    try:
        client().retry_job(job_id)
        flash("已重新入队", "ok")
    except BackendError as exc:
        flash(f"重试失败：{exc.message}", "error")
    return redirect(url_for("jobs.index"))

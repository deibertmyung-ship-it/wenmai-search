"""Job monitoring and retry."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..errors import BackendError
from ._common import as_int, client, job_context

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

@bp.get("/")
def index():
    state = (request.args.get("state") or "").strip()
    limit = as_int(request.args.get("limit"), 100, low=1, high=500)
    api = client()

    return render_template(
        "jobs.html",
        **job_context(api, state=state, limit=limit),
    )


@bp.post("/<job_id>/retry")
def retry(job_id: str):
    try:
        client().retry_job(job_id)
        flash("已重新入队", "ok")
    except BackendError as exc:
        flash(f"重试失败：{exc.message}", "error")
    if request.form.get("return_to") == "jobs.index":
        state = (request.form.get("state") or "").strip()
        return redirect(url_for("jobs.index", state=state or None))
    return redirect(url_for("ingest.index", _anchor="jobs"))

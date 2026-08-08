"""Plagiarism checks: submit, watch, read the report.

The detail route deliberately serves both progress and report from one URL.
That is what lets the no-script path work: a `<meta refresh>` pointed at this
URL turns into the report by itself once the check finishes, with no redirect
logic to get wrong.
"""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack

from flask import (
    Blueprint,
    Response,
    flash,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)

from ..errors import BackendError
from ..report import attach_excerpts, duplication_ratio, numbered_sources, query_spans
from ._common import client

bp = Blueprint("plagiarism", __name__, url_prefix="/plagiarism")

MAX_INPUT_CHARS = 500_000  # mirrors kbsvc plag_max_input_chars
TERMINAL = {"completed", "completed_partial", "failed", "cancelled"}
REPORTABLE = {"completed", "completed_partial"}


@bp.get("/")
def index():
    api = client()
    corpus, corpus_error = _corpus_or_reason(api)
    checks = api.list_checks(limit=50) if corpus_error is None else []
    return render_template(
        "plagiarism.html",
        corpus=corpus,
        corpus_error=corpus_error,
        checks=checks,
        max_chars=MAX_INPUT_CHARS,
        # A fresh token per render: resubmitting the same form hits the same
        # idempotency key, so a double-click or a refresh cannot burn one of
        # the two concurrent-check slots.
        form_token=uuid.uuid4().hex,
    )


@bp.post("/")
def submit():
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("请先粘贴要检测的文字", "error")
        return redirect(url_for("plagiarism.index"))
    if len(text) > MAX_INPUT_CHARS:
        flash(f"超出上限：{len(text):,} 字，最多 {MAX_INPUT_CHARS:,} 字", "error")
        return redirect(url_for("plagiarism.index"))

    try:
        created = client().create_text_check(
            text=text, idempotency_key=request.form.get("form_token") or ""
        )
    except BackendError as exc:
        flash(_explain(exc), "error")
        return redirect(url_for("plagiarism.index"))
    return redirect(url_for("plagiarism.detail", check_id=created["check_id"]))


@bp.post("/documents/<document_id>")
def submit_document(document_id: str):
    try:
        created = client().create_document_check(
            document_id, idempotency_key=request.form.get("form_token") or ""
        )
    except BackendError as exc:
        flash(_explain(exc), "error")
        return redirect(url_for("library.document", document_id=document_id))
    return redirect(url_for("plagiarism.detail", check_id=created["check_id"]))


@bp.get("/checks/<check_id>")
def detail(check_id: str):
    api = client()
    check = api.get_check(check_id)
    status = check["status"]

    report = None
    report_error = None
    if status in REPORTABLE:
        try:
            report = api.get_plag_report(check_id)
        except BackendError as exc:
            if exc.code != "report_visibility_changed":
                raise
            report_error = "来源访问权限已变化，出于安全原因无法显示这份报告。"
    sources = attach_excerpts(api, numbered_sources(report)) if report else []

    rendered = render_template(
        "check.html",
        check=check,
        report=report,
        report_error=report_error,
        sources=sources,
        spans=query_spans(sources),
        ratio=duplication_ratio(report) if report else 0.0,
        live=status not in TERMINAL,
        nojs_refresh_seconds=5,
    )
    return (rendered, 409) if report_error else rendered


@bp.post("/checks/<check_id>/delete")
def delete(check_id: str):
    status = client().delete_check(check_id)
    if status == 202:
        flash("已请求取消，正在停止", "ok")
        return redirect(url_for("plagiarism.detail", check_id=check_id))
    flash("已删除", "ok")
    return redirect(url_for("plagiarism.index"))


def _corpus_or_reason(api) -> tuple[dict | None, str | None]:
    """The corpus call is also how we discover the feature is switched off."""
    try:
        return api.corpus_status(), None
    except BackendError as exc:
        if exc.code == "feature_disabled":
            return None, "当前部署未启用抄袭检测——该功能需要 PostgreSQL。"
        raise


def _explain(exc: BackendError) -> str:
    if exc.code == "plagiarism_concurrency_limit":
        limit = (exc.detail or {}).get("limit", "若干")
        return f"已有 {limit} 个检测在跑。等一个跑完，或到下方列表里取消一个。"
    if exc.code == "plagiarism_text_too_short":
        return f"输入过短：{exc.message}"
    if exc.code in {"plagiarism_input_too_large", "validation_error"}:
        return f"输入不合法：{exc.message}"
    if exc.code == "plagiarism_corpus_not_ready":
        return "语料仍在准备中，请稍后再试。"
    if exc.code == "plagiarism_corpus_empty":
        return "书库为空，请先导入典籍。"
    if exc.code == "idempotency_conflict":
        return "这张表单已用于另一份内容，请返回后重新提交。"
    return f"提交失败：{exc.message}"


# Well under kbsvc's plag_sse_max_seconds (900): one stream pins one waitress
# thread for its whole life, so the cap must come from this side. EventSource
# reconnects on its own and carries Last-Event-ID, which kbsvc resumes from
# exactly - so cutting the stream costs the reader nothing.
PROXY_MAX_SECONDS = 120


@bp.get("/checks/<check_id>/events")
def events(check_id: str):
    api = client()
    last_event_id = request.headers.get("Last-Event-ID", "")

    # Enter upstream before committing the downstream 200 headers. Otherwise a
    # backend 404/503 becomes a fake successful SSE response whose body happens
    # to contain JSON or a late generator exception.
    stack = ExitStack()
    try:
        upstream = stack.enter_context(
            api.stream_progress(check_id, last_event_id=last_event_id)
        )
    except Exception:
        stack.close()
        raise

    @stream_with_context
    def relay():
        # stream_with_context matters: without it the app context pops when
        # this view returns and teardown closes the client mid-stream.
        try:
            deadline = time.monotonic() + PROXY_MAX_SECONDS
            for line in upstream.iter_lines():
                # iter_lines drops the newline; SSE needs it back, and blank
                # lines are what delimit frames.
                yield f"{line}\n"
                if time.monotonic() >= deadline:
                    return
        finally:
            stack.close()

    response = Response(
        relay(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    # Covers clients/middleware that close the response without iterating the
    # generator, in addition to relay()'s finally block.
    response.call_on_close(stack.close)
    return response

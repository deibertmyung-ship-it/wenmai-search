"""Ingestion: source creation, single upload, bulk path registration."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..errors import BackendError
from ._common import client

bp = Blueprint("ingest", __name__, url_prefix="/ingest")


@bp.get("/")
def index():
    return render_template("ingest.html", sources=client().list_sources())


@bp.post("/source")
def create_source():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("来源名称不能为空", "error")
        return redirect(url_for("ingest.index"))

    kind = request.form.get("kind", "upload")
    uri = (request.form.get("uri") or "").strip()
    try:
        source = client().create_source(name, kind=kind, uri=uri)
    except BackendError as exc:
        flash(f"创建失败：{exc.message}", "error")
        return redirect(url_for("ingest.index"))

    flash(f"来源「{source['name']}」已就绪", "ok")
    return redirect(url_for("ingest.index"))


@bp.post("/upload")
def upload():
    source_id = request.form.get("source_id", "")
    upload_file = request.files.get("file")
    if not source_id or not upload_file or not upload_file.filename:
        flash("请选择来源与文件", "error")
        return redirect(url_for("ingest.index"))

    try:
        result = client().upload(
            source_id=source_id,
            filename=upload_file.filename,
            content=upload_file.read(),
            title=(request.form.get("title") or "").strip(),
            acl=(request.form.get("acl") or "").strip(),
        )
    except BackendError as exc:
        flash(f"上传失败：{exc.message}", "error")
        return redirect(url_for("ingest.index"))

    if result.get("deduplicated"):
        flash("内容未变化，已跳过（未产生新版本）", "")
    else:
        flash("已登记并入队，可在任务页查看进度", "ok")
    return redirect(url_for("jobs.index"))


@bp.post("/path")
def ingest_path():
    source_id = request.form.get("source_id", "")
    path = (request.form.get("path") or "").strip()
    if not source_id or not path:
        flash("请填写来源与路径", "error")
        return redirect(url_for("ingest.index"))

    patterns = [p.strip() for p in (request.form.get("patterns") or "*").split(",") if p.strip()]
    try:
        result = client().ingest_path(
            source_id=source_id,
            path=path,
            patterns=patterns,
            recursive=request.form.get("recursive") == "on",
        )
    except BackendError as exc:
        flash(f"批量导入失败：{exc.message}", "error")
        return redirect(url_for("ingest.index"))

    flash(
        f"登记 {result['registered']} 项，去重 {result['deduplicated']} 项，"
        f"失败 {result['failed']} 项",
        "ok" if not result["failed"] else "error",
    )
    return redirect(url_for("jobs.index"))

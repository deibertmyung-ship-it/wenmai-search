"""CLI tests - this is the operator's primary interface, so it gets covered."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from kbsvc.cli import app

runner = CliRunner()


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory):
    """A miniature two-book corpus, including a UTF-16 file like the real one."""
    root = tmp_path_factory.mktemp("cli-corpus")
    (root / "卷一.txt").write_bytes(
        ("卷一\n贼克者，取用之首法也。上克下为贼，下贼上为克。\n" * 4).encode("utf-16")
    )
    (root / "卷二.md").write_text(
        "# 涉害\n\n涉害者，比用不成则涉害。涉害深者为用，历克多者为深。\n" * 4,
        encoding="utf-8",
    )
    (root / "notes.bin").write_bytes(b"\x00\x01\x02")  # excluded by the pattern
    return root


def _run(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output + str(result.exception)
    return result.output


def test_init_reports_the_active_profile():
    assert "profile=local" in _run("init")


def test_add_source_prints_a_stable_id():
    first = _run("add-source", "cli-src", "--kind", "filesystem").strip()
    second = _run("add-source", "cli-src", "--kind", "filesystem").strip()
    assert first == second


def test_issue_key_returns_a_usable_plaintext_key(session, tenant):
    from kbsvc.api.auth import resolve_principal

    raw = _run("issue-key", "cli-key", "--acl", "public,internal").strip()
    assert raw.startswith("kb_")

    principal = resolve_principal(session, raw)
    assert principal.tenant_id == tenant
    assert set(principal.acl) == {"public", "internal"}


def test_ingest_indexes_matching_files_and_skips_the_rest(corpus_dir):
    output = _run("ingest", str(corpus_dir), "--source", "cli-books", "--patterns", "*.txt,*.md")
    assert "registered=2" in output
    assert "failed=0" in output
    assert "worker processed" in output


def test_reingesting_unchanged_files_is_deduplicated(corpus_dir):
    output = _run("ingest", str(corpus_dir), "--source", "cli-books", "--patterns", "*.txt,*.md")
    assert "registered=0" in output
    assert "deduplicated=2" in output


def test_stats_counts_the_ingested_corpus(corpus_dir):
    payload = json.loads(_run("stats"))
    assert payload["documents"] >= 2
    assert payload["chunks"] >= 2
    assert payload["jobs_by_state"].get("completed", 0) >= 2


def test_search_json_output_carries_citations(corpus_dir):
    payload = json.loads(_run("search", "贼克如何取用", "--top-k", "3", "--json"))
    assert payload["results"]
    top = payload["results"][0]
    assert top["chunk_id"] and top["document_id"]
    assert "贼克" in top["snippet"]


def test_search_human_output_is_readable(corpus_dir):
    output = _run("search", "涉害深者为用", "--top-k", "2")
    assert "cite:" in output
    assert "score=" in output


def test_search_debug_flag_emits_the_trace(corpus_dir):
    output = _run("search", "涉害", "--top-k", "1", "--debug", "--json")
    payload = json.loads(output)
    assert payload["debug"]["fusion"]["method"] == "rrf"


def test_search_reports_no_results_without_crashing():
    output = _run("search", "ZZZQQQ-nonexistent-token-xyzzy", "--top-k", "3")
    assert "no results" in output or "cite:" in output


def test_worker_once_on_an_empty_queue_is_a_noop():
    assert "processed 0 job(s)" in _run("worker", "--once")


def test_ingest_a_single_file_path(corpus_dir):
    output = _run("ingest", str(corpus_dir / "卷二.md"), "--source", "cli-single")
    assert "failed=0" in output

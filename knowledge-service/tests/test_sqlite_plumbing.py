"""Tests for SQLite single-file plumbing (ADR-0008 ticket 02).

Covers:
- sqlite-vec extension loading per-connection
- vec0 and fts5 virtual table creation and idempotency
- startup self-check for extension availability
- transactional atomicity across metadata + vec0 + fts5
- unicode61 CJK term behaviour (五行 is a single term)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, inspect, text

from kbsvc.db.session import _make_engine, init_db, reset_engine_cache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FTS_INSERT = (
    "INSERT INTO chunk_fts("
    "chunk_id, tenant_id, document_id, source_id, kind, acl, is_current, body"
    ") VALUES ({vals})"
)


def _make_fresh_sqlite(tmp_path: Path) -> str:
    """Return a database URL for a fresh SQLite file under *tmp_path*."""
    db_file = tmp_path / "test.db"
    return f"sqlite:///{db_file.as_posix()}"


def _table_exists(engine, table_name: str) -> bool:
    inspector = inspect(engine)
    return table_name in inspector.get_table_names()


def _fts_insert(row: dict) -> str:
    vals = ", ".join(f"'{v}'" for v in (
        row["chunk_id"], row["tenant_id"], row["document_id"],
        row["source_id"], row["kind"], row["acl"],
        str(row["is_current"]), row["body"],
    ))
    return _FTS_INSERT.format(vals=vals)


# ---------------------------------------------------------------------------
# 1. sqlite-vec extension loading
# ---------------------------------------------------------------------------

class TestExtensionLoading:
    """The connect listener must load sqlite-vec on every new connection
    when KB_VECTOR_BACKEND=sqlite-vec."""

    def test_vec0_function_available_after_connect(self, tmp_path, monkeypatch):
        """A connection from the engine should have the vec0 module loaded."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_VECTOR_BACKEND", "sqlite-vec")
        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        try:
            engine = _make_engine(url)
            with engine.connect() as conn:
                conn.execute(text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS _test_vec "
                    "USING vec0(embedding float[4])"
                ))
                conn.commit()
            engine.dispose()
        finally:
            reset_settings_cache()

    def test_extension_loaded_on_every_connection(self, tmp_path, monkeypatch):
        """Each new connection must load the extension, not just the first."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_VECTOR_BACKEND", "sqlite-vec")
        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        try:
            engine = _make_engine(url)
            with engine.connect() as conn1:
                conn1.execute(text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS _t "
                    "USING vec0(v float[2])"
                ))
                conn1.commit()
            with engine.connect() as conn2:
                conn2.execute(text(
                    "INSERT INTO _t(rowid, v) VALUES (1, x'0000803f0000003f')"
                ))
                conn2.commit()
            engine.dispose()
        finally:
            reset_settings_cache()

# ---------------------------------------------------------------------------
# 2. Startup self-check
# ---------------------------------------------------------------------------

class TestExtensionSelfCheck:
    """Missing sqlite-vec should produce an actionable error at startup."""

    def test_missing_sqlite_vec_raises_actionable_error(self, tmp_path):
        """When sqlite-vec cannot be loaded, the error message names the
        package and the CLI command to install it."""
        import builtins

        real_import = builtins.__import__

        def _block_sqlite_vec(name, *args, **kwargs):
            if name == "sqlite_vec":
                raise ImportError("simulated: no module named 'sqlite_vec'")
            return real_import(name, *args, **kwargs)

        url = _make_fresh_sqlite(tmp_path)
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _listener(dbapi_conn, _record):
            with patch("builtins.__import__", side_effect=_block_sqlite_vec):
                from kbsvc.db.session import _load_sqlite_vec_extension
                _load_sqlite_vec_extension(dbapi_conn)

        try:
            with pytest.raises(RuntimeError) as exc_info, engine.connect():
                pass

            msg = str(exc_info.value)
            assert "sqlite-vec" in msg.lower()
            assert "pip install" in msg.lower()
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# 3. Virtual table DDL via init_db
# ---------------------------------------------------------------------------

class TestVirtualTableCreation:
    """init_db creates metadata + FTS5 tables; ensure_vec0_table creates the
    vec0 virtual table with a runtime-discovered dimension."""

    def test_init_db_creates_metadata_tables(self, tmp_path, monkeypatch):
        """init_db creates the standard metadata tables and is idempotent."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_DATABASE_URL", url)
        monkeypatch.setenv("KB_PROFILE", "local")
        monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))

        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        reset_engine_cache()

        try:
            init_db()
            engine = _make_engine(url)
            assert _table_exists(engine, "chunk")
            assert _table_exists(engine, "document")
            assert _table_exists(engine, "tenant")

            init_db()
            assert _table_exists(engine, "chunk")
        finally:
            reset_engine_cache()
            reset_settings_cache()

    def test_ensure_collection_creates_vec0_table(self, tmp_path, monkeypatch):
        """ensure_vec0_table(dim) creates the chunk_vec vec0 virtual table."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_DATABASE_URL", url)
        monkeypatch.setenv("KB_PROFILE", "local")
        monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("KB_VECTOR_BACKEND", "sqlite-vec")

        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        reset_engine_cache()

        try:
            init_db()
            engine = _make_engine(url)

            from kbsvc.db.vec_ddl import ensure_vec0_table
            ensure_vec0_table(engine, dim=4)

            assert _table_exists(engine, "chunk_vec")

            ensure_vec0_table(engine, dim=4)
            assert _table_exists(engine, "chunk_vec")
        finally:
            reset_engine_cache()
            reset_settings_cache()

    def test_fts5_table_created_by_init_db(self, tmp_path, monkeypatch):
        """init_db creates the chunk_fts FTS5 virtual table."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_DATABASE_URL", url)
        monkeypatch.setenv("KB_PROFILE", "local")
        monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))

        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        reset_engine_cache()

        try:
            init_db()
            engine = _make_engine(url)

            assert _table_exists(engine, "chunk_fts")

            init_db()
            assert _table_exists(engine, "chunk_fts")
        finally:
            reset_engine_cache()
            reset_settings_cache()


# ---------------------------------------------------------------------------
# 4. Transactional atomicity
# ---------------------------------------------------------------------------

class TestTransactionalAtomicity:
    """A transaction writing metadata + vec0 + fts5 must roll back all three
    on a mid-transaction exception."""

    def test_mid_exception_rolls_back_all_three(self, tmp_path, monkeypatch):
        """Write to metadata, chunk_vec, and chunk_fts in one transaction;
        raise before commit; verify none of the writes persist."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_DATABASE_URL", url)
        monkeypatch.setenv("KB_PROFILE", "local")
        monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("KB_VECTOR_BACKEND", "sqlite-vec")

        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        reset_engine_cache()

        try:
            init_db()
            engine = _make_engine(url)

            from kbsvc.db.vec_ddl import ensure_vec0_table
            ensure_vec0_table(engine, dim=2)

            # Pre-existing sentinel row that should survive the rollback.
            sentinel = {
                "chunk_id": "sentinel", "tenant_id": "t",
                "document_id": "d0", "source_id": "s0",
                "kind": "section", "acl": "public",
                "is_current": 1, "body": "pre-existing",
            }
            with engine.begin() as conn:
                conn.execute(text(_fts_insert(sentinel)))

            # Transaction that writes to all three tables then fails.
            chunk_insert = (
                "INSERT INTO chunk("
                "id, tenant_id, document_id, version_id, ordinal, kind, text, "
                "token_count, char_start, char_end, section_id, heading_path, "
                "content_hash, analyzed, created_at"
                ") VALUES ("
                "'c1', 't', 'd1', 'v1', 0, 'section', 'hello', "
                "0, 0, 5, '', '[]', 'hash1', '', '2026-01-01 00:00:00'"
                ")"
            )
            vec_insert = (
                "INSERT INTO chunk_vec("
                "rowid, embedding, chunk_id, document_id, version_id, "
                "source_id, kind, is_current, tenant"
                ") VALUES ("
                "1, x'0000803f00000000', 'c1', 'd1', 'v1', "
                "'s1', 'section', 1, 't'"
                ")"
            )
            new_row = {
                "chunk_id": "c1", "tenant_id": "t",
                "document_id": "d1", "source_id": "s1",
                "kind": "section", "acl": "public",
                "is_current": 1, "body": "hello world",
            }
            with pytest.raises(RuntimeError, match="simulated failure"), \
                    engine.begin() as conn:
                conn.execute(text(chunk_insert))
                conn.execute(text(vec_insert))
                conn.execute(text(_fts_insert(new_row)))
                raise RuntimeError("simulated failure")

            # Verify none of the transactional writes persisted.
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT count(*) FROM chunk WHERE id = 'c1'")
                ).scalar()
                assert result == 0, "chunk row should have rolled back"

                result = conn.execute(
                    text("SELECT count(*) FROM chunk_vec WHERE rowid = 1")
                ).scalar()
                assert result == 0, "vec0 row should have rolled back"

                result = conn.execute(
                    text("SELECT count(*) FROM chunk_fts WHERE chunk_id = 'c1'")
                ).scalar()
                assert result == 0, "fts5 row should have rolled back"

                result = conn.execute(
                    text("SELECT count(*) FROM chunk_fts WHERE chunk_id = 'sentinel'")
                ).scalar()
                assert result == 1, "pre-existing fts5 row should survive"
        finally:
            reset_engine_cache()
            reset_settings_cache()


# ---------------------------------------------------------------------------
# 5. unicode61 CJK term behaviour
# ---------------------------------------------------------------------------

class TestUnicode61CjkTerms:
    """五行 must be a single term in chunk_fts; MATCH '行' must not hit it."""

    def test_wuxing_is_single_term(self, tmp_path, monkeypatch):
        """The unicode61 tokenizer treats contiguous CJK characters as one
        term, so 五行 is indexed as a single token, not 五 + 行."""
        url = _make_fresh_sqlite(tmp_path)
        monkeypatch.setenv("KB_DATABASE_URL", url)
        monkeypatch.setenv("KB_PROFILE", "local")
        monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "data"))

        from kbsvc.config import reset_settings_cache
        reset_settings_cache()
        reset_engine_cache()

        try:
            init_db()
            engine = _make_engine(url)

            row = {
                "chunk_id": "c1", "tenant_id": "t",
                "document_id": "d1", "source_id": "s1",
                "kind": "section", "acl": "public",
                "is_current": 1, "body": "五行",
            }
            with engine.begin() as conn:
                conn.execute(text(_fts_insert(row)))

            with engine.connect() as conn:
                # MATCH '五行' hits.
                hits = conn.execute(
                    text("SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH '五行'")
                ).scalar()
                assert hits == 1, "MATCH '五行' should hit the row"

                # MATCH '行' does NOT hit, because 五行 is a single term.
                hits = conn.execute(
                    text("SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH '行'")
                ).scalar()
                assert hits == 0, "MATCH '行' should not hit only-五行 row"

                # Also verify body column search.
                hits = conn.execute(
                    text("SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'body:行'")
                ).scalar()
                assert hits == 0, "MATCH 'body:行' should not hit only-五行 row"
        finally:
            reset_engine_cache()
            reset_settings_cache()

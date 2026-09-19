"""Tests for the Qdrant -> consolidated vector export (ADR-0008 ticket 09).

The migration copies stored float32 vectors bit-for-bit rather than
re-embedding, so these tests assert bit-exact transfer, idempotency and
resumability against an embedded Qdrant source and a fresh sqlite-vec target.
The dimension is deliberately small (8) and differs from KB_DENSE_DIM (128)
so a regression that read the width from config instead of the source
collection would fail the upsert, not silently mis-store.
"""

from __future__ import annotations

import struct
import uuid

import pytest

from kbsvc.config import Settings
from kbsvc.vector.base import VectorPoint
from kbsvc.vector.migrate import migrate_vectors
from kbsvc.vector.qdrant_store import QdrantVectorStore

_DIM = 8


def _chunk_id(i: int) -> str:
    # Deterministic valid UUID: embedded Qdrant rejects non-UUID string point
    # ids, and production chunk ids are UUID strings, so use that shape.
    return str(uuid.UUID(int=i + 1))


def _f32(i: int) -> float:
    """A deterministic, exactly-representable float32 value for index *i*.

    Built from integer components via /4.0 so the value survives a
    float32 -> float64 -> float32 round trip unchanged (divisors that are
    powers of two are exact in binary floating point); the bit-exact
    assertion is then genuinely checking storage width, not arithmetic.
    """
    return (i % 64) / 4.0


def _vector(i: int) -> list[float]:
    return [_f32(i + j) for j in range(_DIM)]


@pytest.fixture
def source_store(tmp_path):
    """An embedded Qdrant collection seeded with 150 known vectors."""
    settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "qdrant-src"),
        qdrant_collection="migrate_src",
        qdrant_url="",
    )
    store = QdrantVectorStore(settings)
    store.ensure_collection(_DIM)
    points = [
        VectorPoint(
            id=_chunk_id(i),
            dense=_vector(i),
            payload={
                "tenant_id": "test",
                "document_id": f"doc-{i % 5}",
                "version_id": f"ver-{i % 3}",
                "source_id": "src",
                "kind": "text",
                "is_current": True,
                "ordinal": i,
            },
        )
        for i in range(150)
    ]
    store.upsert(points)
    yield store
    store.close()


@pytest.fixture
def target_env(monkeypatch, tmp_path):
    """Point the global engine at a fresh SQLite file for the sqlite-vec target."""
    db_path = tmp_path / "target.db"
    monkeypatch.setenv("KB_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("KB_PROFILE", "local")
    monkeypatch.setenv("KB_DATA_DIR", str(tmp_path / "tdata"))
    monkeypatch.setenv("KB_VECTOR_BACKEND", "sqlite-vec")
    from kbsvc.config import reset_settings_cache
    from kbsvc.db.session import reset_engine_cache

    reset_settings_cache()
    reset_engine_cache()
    yield
    monkeypatch.undo()
    reset_settings_cache()
    reset_engine_cache()


def _target_bytes(conn, chunk_id: str) -> bytes:
    from sqlalchemy import text

    row = conn.execute(
        text("SELECT embedding FROM chunk_vec WHERE chunk_id = :id"),
        {"id": chunk_id},
    ).first()
    assert row is not None, f"chunk {chunk_id} missing from target"
    return bytes(row[0])


def test_migration_is_bit_exact_and_count_matches(source_store, target_env):
    from kbsvc.db.session import session_scope

    result = migrate_vectors(
        settings=source_store.settings,
        source=source_store,
        target_backend="sqlite-vec",
        batch_size=50,
        sample_size=100,
    )

    assert result.ok
    assert result.dim == _DIM
    assert result.source_count == 150
    assert result.target_count == 150
    assert result.migrated == 150
    assert result.sampled == 100
    assert result.bit_exact

    # Independently re-read every target vector and compare raw bytes against
    # what the SOURCE stores - not the original input. Qdrant normalizes cosine
    # vectors to unit length on upsert, so the stored float32 differs from the
    # pre-normalization value; cosine top-k is invariant to that normalization,
    # so bit-exact against the source's *stored* vector is the correct invariant.
    # The CLI's own sample is 100; pin all 150 here so a width bug past the
    # sample window cannot hide.
    from kbsvc.vector.qdrant_store import DENSE_VECTOR

    recs, _ = source_store.client.scroll(
        collection_name=source_store.collection,
        limit=150,
        with_vectors=True,
        with_payload=False,
    )
    source_bytes = {}
    for r in recs:
        vec = r.vector[DENSE_VECTOR] if isinstance(r.vector, dict) else r.vector
        source_bytes[str(r.id)] = struct.pack(
            f"<{_DIM}f", *[float(x) for x in vec]
        )
    with session_scope() as session:
        conn = session.connection()
        for i in range(150):
            chunk_id = _chunk_id(i)
            assert _target_bytes(conn, chunk_id) == source_bytes[chunk_id], chunk_id


def test_migration_is_idempotent_on_rerun(source_store, target_env):
    first = migrate_vectors(
        settings=source_store.settings,
        source=source_store,
        target_backend="sqlite-vec",
        batch_size=75,
    )
    second = migrate_vectors(
        settings=source_store.settings,
        source=source_store,
        target_backend="sqlite-vec",
        batch_size=75,
    )

    assert first.ok and second.ok
    # No duplicate rows: count stays at the source count after the second run.
    assert second.target_count == 150
    assert second.source_count == 150


def test_migration_resumes_after_point_id(source_store, target_env):
    # First pass drains the whole source.
    first = migrate_vectors(
        settings=source_store.settings,
        source=source_store,
        target_backend="sqlite-vec",
        batch_size=100,
        sample_size=0,
    )
    assert first.migrated == 150

    # Simulate a resume: scroll continues after the checkpoint point. The upsert
    # is idempotent, so this must not error or duplicate; it re-writes the tail.
    checkpoint = _chunk_id(49)
    resumed = migrate_vectors(
        settings=source_store.settings,
        source=source_store,
        target_backend="sqlite-vec",
        batch_size=50,
        resume_after=checkpoint,
        sample_size=50,
    )

    assert resumed.ok
    assert resumed.target_count == 150
    # The checkpoint itself is excluded (scroll offset is exclusive), and at
    # least one point precedes it, so the tail is a non-empty proper subset.
    assert 0 < resumed.migrated < 150
    # The resume's own bit-exact sample (prefix of the source) must pass.
    assert resumed.bit_exact


def test_migration_rejects_missing_source_collection(tmp_path, target_env):
    from kbsvc.errors import KbError

    # Source settings point at an embedded Qdrant dir whose collection was never
    # created. The target store ignores these settings for its own connection
    # (it uses the global engine set up by target_env), so passing them straight
    # to migrate_vectors steers only the source side.
    empty_settings = Settings(
        _env_file=None,
        data_dir=str(tmp_path / "qdrant-empty"),
        qdrant_collection="does_not_exist",
        qdrant_url="",
    )
    with pytest.raises(KbError):
        migrate_vectors(
            settings=empty_settings,
            target_backend="sqlite-vec",
        )




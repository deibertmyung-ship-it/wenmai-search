"""One-time vector export migration: Qdrant -> sqlite-vec/pgvector.

ADR-0008 ticket 09. This moves the *existing* dense vectors out of Qdrant
bit-for-bit - it does not re-embed. Re-embedding would introduce ONNX
non-determinism and batch effects, which would make the acceptance gate's
"local dense top-k set equals the old stack" hard assertion fail for reasons
that have nothing to do with storage, making it impossible to tell a
migration bug from float jitter. Export is minute-scale, not the ~2 hours a
full re-embed of 22k chunks costs.

Lexical is deliberately not migrated here: `rebuild-lexical` rebuilds the
FTS5/pg_search index from the `chunk` table in ~21 seconds, with body and
analysed text already in the database. Writing a lexical export path would
be wasted code.

Shape mirrors `ingest/reembed.py`: keyset pagination, per-batch transaction,
idempotent upsert, resumable via `--resume-after`. The difference is the
source is Qdrant's scroll API (with `with_vectors=True`) rather than the
chunk table, and the dimension is read from the source collection config -
never `KB_DENSE_DIM`, whose default (384) does not match production (512 from
bge-small-zh-v1.5).
"""

from __future__ import annotations

import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text

from ..config import Settings
from ..db.session import session_scope
from ..errors import KbError
from .base import VectorPoint
from .qdrant_store import DENSE_VECTOR, QdrantVectorStore

logger = logging.getLogger(__name__)

ProgressHook = Callable[[int, int], None]

# Number of migrated chunks whose vectors are re-read from the target after
# the run and compared to the source float32-by-float32. count() equality
# alone cannot catch a float-width bug (a vector stored as float64 still
# counts as one row); the per-dimension sample is what catches a float32 ->
# float64 -> float32 round trip through any layer of the stack.
SAMPLE_SIZE = 100


@dataclass
class MigrationResult:
    migrated: int
    dim: int
    source_count: int
    target_count: int
    sampled: int
    bit_exact: bool
    elapsed_seconds: float
    last_point_id: str = ""  # pass to --resume-after if interrupted

    @property
    def count_match(self) -> bool:
        return self.source_count == self.target_count

    @property
    def ok(self) -> bool:
        return self.count_match and self.bit_exact


def _source_dimension(source: QdrantVectorStore) -> int:
    """Read the vector width from the source collection config.

    Must not trust `KB_DENSE_DIM`: its default 384 is the hash provider's
    width, while production uses bge-small-zh-v1.5 at 512. The collection
    config is the only authoritative source for what is actually stored.
    """
    info = source.client.get_collection(source.collection)
    params = info.config.params.vectors
    # Named vectors return a dict mapping name -> VectorParams; an unnamed
    # vector returns the VectorParams directly. This collection always uses
    # the named DENSE_VECTOR, but handle both for resilience.
    cfg = params.get(DENSE_VECTOR) if isinstance(params, dict) else params
    if cfg is None:
        raise KbError(
            "source Qdrant collection has no dense vector named "
            f"{DENSE_VECTOR!r}",
            {"available": list(params) if isinstance(params, dict) else None},
        )
    return int(cfg.size)


def _record_vector(record, dim: int) -> list[float]:
    """Extract the dense vector from a scroll record as a plain float list."""
    vector = record.vector
    if isinstance(vector, dict):
        vector = vector[DENSE_VECTOR]
    return [float(x) for x in vector]


def _f32_bytes(values: list[float], dim: int) -> bytes:
    """Canonical little-endian float32 encoding for bit-exact comparison."""
    return struct.pack(f"<{dim}f", *values)


def _to_point(record, dim: int) -> VectorPoint:
    point_id = str(record.id)
    payload = dict(record.payload or {})
    return VectorPoint(id=point_id, dense=_record_vector(record, dim), payload=payload)


def _build_target(backend: str, settings: Settings):
    """Construct the target store by name, bypassing the process singleton.

    The migration runs in a process whose `KB_VECTOR_BACKEND` may not name
    the target yet (cutover happens after a verified migration), so the
    store is built explicitly rather than through `get_vector_store()`.
    """
    if backend == "sqlite-vec":
        from .sqlite_vec_store import SqliteVecStore

        return SqliteVecStore(settings)
    if backend == "pgvector":
        from .pgvector_store import PgVectorStore

        return PgVectorStore(settings)
    raise KbError(
        f"unknown migration target {backend!r}; expected 'sqlite-vec' or 'pgvector'"
    )


def migrate_vectors(
    *,
    settings: Settings,
    target_backend: str,
    batch_size: int = 1000,
    resume_after: str | None = None,
    sample_size: int = SAMPLE_SIZE,
    progress: ProgressHook | None = None,
    source: QdrantVectorStore | None = None,
) -> MigrationResult:
    """Export every vector from Qdrant into *target_backend*.

    Idempotent: both in-database targets upsert by chunk id, so re-running a
    completed migration writes the same vectors again without duplicating
    rows. Resumable: `resume_after` (a point id, exclusive) is passed
    straight to Qdrant's scroll offset, mirroring `reembed`'s `--resume-after`.

    `source` may be a pre-built `QdrantVectorStore` - useful when the caller
    already holds the embedded-Qdrant directory lock (a second client on the
    same path is rejected); when omitted one is built from *settings* and
    closed before returning.

    Finishes with two verifications: source and target `count()` must agree,
    and a sample of vectors must be bit-identical float32 across the two.
    Returns a `MigrationResult`; the caller (CLI) is responsible for a
    non-zero exit when `not result.ok`.
    """
    started = time.monotonic()
    owns_source = source is None
    source = source or QdrantVectorStore(settings)
    target = _build_target(target_backend, settings)
    try:
        if not source.client.collection_exists(source.collection):
            raise KbError(
                f"source Qdrant collection {source.collection!r} does not exist"
            )
        dim = _source_dimension(source)
        logger.info(
            "migrating %s (%s) -> %s, dim=%d, batch=%d",
            source.collection,
            "embedded" if settings.use_embedded_qdrant else settings.qdrant_url,
            target_backend,
            dim,
            batch_size,
        )
        # ensure_collection (not recreate): a resumed or re-run must not drop
        # vectors already written. A dimension mismatch with an existing target
        # raises here rather than silently writing wrong-width vectors.
        target.ensure_collection(dim)

        source_count = source.count()
        offset = resume_after or None
        migrated = 0
        last_id = resume_after or ""

        while True:
            records, next_offset = source.client.scroll(
                collection_name=source.collection,
                limit=batch_size,
                offset=offset,
                with_vectors=True,
                with_payload=True,
            )
            if not records:
                break
            points = [_to_point(record, dim) for record in records]
            last_id = points[-1].id

            # One short transaction per batch, same pattern reembed uses: the
            # slow part (Qdrant scroll over the network) is already done by the
            # time we open this, so the write lock is held only for the upsert.
            with session_scope() as session:
                target.upsert(points, session=session)

            migrated += len(points)
            if progress:
                progress(migrated, source_count)
            if next_offset is None:
                break
            offset = next_offset

        target_count = target.count()
        sampled, bit_exact = _verify_bit_exact(
            source, target, target_backend, dim, sample_size
        )
    finally:
        close = getattr(target, "close", None)
        if close is not None:
            close()
        if owns_source:
            source.close()

    elapsed = time.monotonic() - started
    return MigrationResult(
        migrated=migrated,
        dim=dim,
        source_count=source_count,
        target_count=target_count,
        sampled=sampled,
        bit_exact=bit_exact,
        elapsed_seconds=elapsed,
        last_point_id=last_id,
    )


def _verify_bit_exact(
    source: QdrantVectorStore,
    target,
    target_backend: str,
    dim: int,
    sample_size: int,
) -> tuple[int, bool]:
    """Sample up to *sample_size* source chunks and compare float32 bytes.

    Returns (sampled_count, all_equal). The sample is the first N chunks by
    Qdrant scroll order - deterministic, and float-width corruption is not
    position-dependent, so a spread sample buys nothing over a prefix for
    this particular check. A chunk missing from the target fails the check.
    """
    if sample_size <= 0:
        return 0, True
    records, _ = source.client.scroll(
        collection_name=source.collection,
        limit=sample_size,
        with_vectors=True,
        with_payload=False,
    )
    if not records:
        return 0, True

    expected = {str(r.id): _f32_bytes(_record_vector(r, dim), dim) for r in records}
    actual = _read_target_vectors(target, target_backend, list(expected.keys()), dim)

    all_equal = True
    for point_id, want in expected.items():
        got = actual.get(point_id)
        if got != want:
            all_equal = False
            logger.error(
                "vector mismatch for chunk %s: target has %s",
                point_id,
                "no vector" if got is None else "different float32 bytes",
            )
    return len(expected), all_equal


def _read_target_vectors(
    target, target_backend: str, ids: list[str], dim: int
) -> dict[str, bytes]:
    """Read back stored vectors as canonical float32 bytes, keyed by chunk id.

    Target-backend-specific because each store owns a different table shape:
    sqlite-vec keeps a blob in `chunk_vec.embedding`, pgvector keeps a
    `vector` column on `chunk`. This is a verification-only path; it does
    not widen the runtime VectorStore Protocol with a read method the
    serving path never needs.
    """
    if target_backend == "sqlite-vec":
        sql = text("SELECT chunk_id, embedding FROM chunk_vec WHERE chunk_id = :id")
    else:  # pgvector
        sql = text("SELECT id, embedding FROM chunk WHERE id = :id")

    out: dict[str, bytes] = {}
    with session_scope() as session:
        conn = session.connection()
        for point_id in ids:
            row = conn.execute(sql, {"id": point_id}).first()
            if row is None or row[1] is None:
                continue
            if target_backend == "sqlite-vec":
                # Stored exactly as _vector_to_blob produced it - little-endian
                # float32. Compare to the source's canonical bytes directly.
                out[point_id] = bytes(row[1])
            else:
                # register_vector decodes the `vector` column to a numpy
                # float32 array; repack to canonical bytes so the comparison
                # is against the stored float32 width, not Python float64.
                out[point_id] = _f32_bytes([float(x) for x in row[1]], dim)
    return out

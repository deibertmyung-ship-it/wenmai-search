"""PostgreSQL models for plagiarism detection.

On a **separate declarative base** from the rest of kbsvc. The main `Base` is
created by `init_db()` on every profile including SQLite, and these tables use
`BIGINT[]` and GIN indexes that SQLite cannot express. Sharing a base would
make `create_all` fail on a local install of a feature that install does not
even offer.

These tables are a *derived projection*, not a source of truth. Documents,
versions, ACLs, original files and the retrieval indexes stay owned by the
existing modules; everything here can be rebuilt from them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PlagiarismBase(DeclarativeBase):
    """Independent metadata registry - see the module docstring."""


class PlagCorpusProjection(PlagiarismBase):
    """One built projection of one document version.

    `active_from` / `active_until` give the projection a validity window. A
    check records `snapshot_at` and only ever reads projections whose window
    contains it, so a rebuild that lands mid-check cannot change that check's
    corpus underneath it.
    """

    __tablename__ = "plag_corpus_projection"
    __table_args__ = (
        # One live projection per (document, algorithm) at a time. Retired rows
        # keep their `active_until` set, so the partial index below is what
        # actually enforces "one active".
        Index(
            "ix_plag_projection_lookup",
            "tenant_id",
            "document_id",
            "algorithm_config_hash",
        ),
        Index("ix_plag_projection_window", "tenant_id", "active_from", "active_until"),
        Index("ix_plag_projection_version", "version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    # Controlled reference into the knowledge base. Deliberately not a foreign
    # key: this table is a rebuildable projection and must never block a
    # document operation.
    document_id: Mapped[str] = mapped_column(String(36))
    version_id: Mapped[str] = mapped_column(String(36))
    content_hash: Mapped[str] = mapped_column(String(64))

    language: Mapped[str] = mapped_column(String(16), default="en")
    # ACL snapshot taken at build time. Candidate queries filter on the live
    # document ACL as well; this copy exists so a projection can be reasoned
    # about without joining back.
    acl: Mapped[list] = mapped_column(JSON, default=list)
    algorithm_config_hash: Mapped[str] = mapped_column(String(32), index=True)

    total_chars: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    active_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlagCorpusChunk(PlagiarismBase):
    """A sliding-window chunk of a projection, with its winnowing fingerprints.

    `fingerprints` is the GIN-indexed array the L1 candidate query overlaps
    against. `char_start` / `char_end` are offsets into the *document* text, so
    a finding can be reported without re-deriving them.
    """

    __tablename__ = "plag_corpus_chunk"
    __table_args__ = (
        UniqueConstraint("projection_id", "chunk_index", name="uq_plag_chunk_position"),
        # The candidate query is `fingerprints && :probe`; without this index it
        # degrades to a sequential scan over the whole corpus. ADR-0001 rules
        # out a Python fallback, so this index is load-bearing, not an
        # optimisation.
        Index(
            "ix_plag_chunk_fingerprints_gin",
            "fingerprints",
            postgresql_using="gin",
        ),
        Index("ix_plag_chunk_projection", "projection_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    projection_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    sentence_count: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    fingerprints: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)


class PlagFingerprintDf(PlagiarismBase):
    """Document frequency per fingerprint, for stop-listing.

    A fingerprint occurring in a large fraction of the corpus carries no signal
    (boilerplate, stock phrases) and would otherwise dominate candidate
    retrieval.
    """

    __tablename__ = "plag_fingerprint_df"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    algorithm_config_hash: Mapped[str] = mapped_column(String(32), primary_key=True)
    fingerprint: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlagCorpusJob(PlagiarismBase):
    """Work item: build (or rebuild) the projection for one document version."""

    __tablename__ = "plag_corpus_job"
    __table_args__ = (
        # Idempotent registration: the ingest hook may fire more than once for
        # the same version, and a backfill re-registers everything.
        UniqueConstraint(
            "tenant_id",
            "version_id",
            "algorithm_config_hash",
            name="uq_plag_corpus_job_target",
        ),
        Index("ix_plag_corpus_job_claim", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[str] = mapped_column(String(36))
    version_id: Mapped[str] = mapped_column(String(36))
    algorithm_config_hash: Mapped[str] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    leased_by: Mapped[str] = mapped_column(String(128), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PlagCheck(PlagiarismBase):
    """One detection job, its snapshot, and its rolled-up result."""

    __tablename__ = "plag_check"
    __table_args__ = (
        # Idempotency is scoped to the caller *and* the route, so the same key
        # on a different endpoint is a different request, not a conflict.
        #
        # PARTIAL on purpose. The key is optional, and a plain unique constraint
        # treats every un-keyed check as having the same key `''` - which makes
        # a caller's second ordinary request collide with their first. The
        # constraint must only bind when a key was actually supplied.
        Index(
            "uq_plag_check_idempotency",
            "tenant_id",
            "creator_key_id",
            "route",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key <> ''"),
        ),
        Index("ix_plag_check_owner", "tenant_id", "creator_key_id", "created_at"),
        Index("ix_plag_check_claim", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    # Ownership is per API key, not per tenant: a check is visible only to the
    # credential that created it.
    creator_key_id: Mapped[str] = mapped_column(String(36))
    route: Mapped[str] = mapped_column(String(64), default="")
    idempotency_key: Mapped[str] = mapped_column(String(255), default="")
    request_digest: Mapped[str] = mapped_column(String(64), default="")

    # Text mode stores the submitted text; document mode stores a reference and
    # leaves `query_text` empty so the original is not duplicated.
    query_text: Mapped[str] = mapped_column(Text, default="")
    query_chars: Mapped[int] = mapped_column(Integer, default=0)
    source_document_id: Mapped[str] = mapped_column(String(36), default="")
    source_version_id: Mapped[str] = mapped_column(String(36), default="")
    # Every version of this document is excluded from candidates - excluding
    # only the current one would let an earlier revision match itself.
    excluded_document_id: Mapped[str] = mapped_column(String(36), default="")
    language: Mapped[str] = mapped_column(String(16), default="auto")
    acl: Mapped[list] = mapped_column(JSON, default=list)

    snapshot_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    algorithm_config_hash: Mapped[str] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(24), default="pending")
    cancel_requested: Mapped[bool] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    leased_by: Mapped[str] = mapped_column(String(128), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")

    matched_chars: Mapped[int] = mapped_column(Integer, default=0)
    checked_chunks: Mapped[int] = mapped_column(Integer, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    coverage_reason: Mapped[str] = mapped_column(String(24), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlagCheckSource(PlagiarismBase):
    """Per-source rollup for one check. Version and hash are frozen at run time
    so the finding stays checkable after the document moves on."""

    __tablename__ = "plag_check_source"
    __table_args__ = (
        UniqueConstraint("check_id", "document_id", name="uq_plag_check_source"),
        Index("ix_plag_check_source_check", "check_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    check_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[str] = mapped_column(String(36))
    version_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    matched_chars: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)


class PlagCheckPassage(PlagiarismBase):
    """One aligned span. Offsets are in *document* coordinates on both sides.

    `preview` is capped at the configured preview length - a report must not
    become a way to read source documents around the ACL check.
    """

    __tablename__ = "plag_check_passage"
    __table_args__ = (Index("ix_plag_check_passage_source", "check_id", "source_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    check_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    query_start: Mapped[int] = mapped_column(Integer)
    query_end: Mapped[int] = mapped_column(Integer)
    source_start: Mapped[int] = mapped_column(Integer)
    source_end: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    preview: Mapped[str] = mapped_column(Text, default="")


class PlagCheckEvent(PlagiarismBase):
    """Persisted progress event - the SSE source of truth.

    `id` is a database-assigned monotonic bigint used directly as the SSE `id:`
    field, which is what makes `Last-Event-ID` resumption exact. A UUID would
    not order.
    """

    __tablename__ = "plag_check_event"
    __table_args__ = (Index("ix_plag_check_event_stream", "check_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    check_id: Mapped[str] = mapped_column(String(36))
    tenant_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # Never carries submitted text, source text, credentials or stack traces.
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlagWorkerHeartbeat(PlagiarismBase):
    """Liveness of each plagiarism worker, for the readiness check."""

    __tablename__ = "plag_worker_heartbeat"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="")
    algorithm_config_hash: Mapped[str] = mapped_column(String(32), default="")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


__all__ = [
    "PlagCheck",
    "PlagCheckEvent",
    "PlagCheckPassage",
    "PlagCheckSource",
    "PlagCorpusChunk",
    "PlagCorpusJob",
    "PlagCorpusProjection",
    "PlagFingerprintDf",
    "PlagWorkerHeartbeat",
    "PlagiarismBase",
    "utcnow",
]

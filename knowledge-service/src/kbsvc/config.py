"""Runtime configuration. Everything is driven by KB_* environment variables."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Profile = Literal["local", "server"]
DenseProvider = Literal["hash", "fastembed", "openai"]
Reranker = Literal["none", "lexical", "cross-encoder"]
VectorBackend = Literal["qdrant", "sqlite-vec", "pgvector"]
LexicalBackend = Literal["tantivy", "fts5", "pg-search"]

DEFAULT_DATA_DIR = Path.cwd() / ".kbdata"

# Backends that live inside the local SQLite file, and backends that live in a
# PostgreSQL database (ADR-0008). Membership decides which deployments can
# serve a switch at all.
_SQLITE_EMBEDDED_BACKENDS = frozenset({"sqlite-vec", "fts5"})
_POSTGRES_BACKENDS = frozenset({"pgvector", "pg-search"})
# Stores this process opens itself instead of reaching over the network.
# Embedded Qdrant belongs here too, but only when `qdrant_url` is empty, so it
# is decided separately.
_IN_PROCESS_BACKENDS = frozenset({"sqlite-vec", "fts5", "tantivy"})


def _is_postgres_url(url: str) -> bool:
    """Whether `url` names a PostgreSQL database, whatever driver it asks for.

    `postgres://` is the legacy spelling SQLAlchemy dropped in 1.4, and it is
    accepted here on purpose: the question this answers is which engine backs
    the deployment, not whether the URL parses. Rejecting it would report a
    backend as unreachable when the real fault is one character in the scheme;
    `create_engine` refuses it a moment later, at startup, and says so.
    """
    scheme = url.split("://", 1)[0].split("+", 1)[0].lower()
    return scheme in {"postgres", "postgresql"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KB_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    profile: Profile = "local"
    data_dir: Path = DEFAULT_DATA_DIR
    default_tenant: str = "default"

    # --- metadata store -------------------------------------------------
    # Empty means "derive from profile": sqlite under data_dir for local.
    database_url: str = ""

    # --- object storage -------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    storage_root: Path | None = None
    s3_endpoint_url: str = ""
    s3_bucket: str = "kbsvc"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # --- retrieval backends ---------------------------------------------
    # Which implementation backs each half of retrieval (ADR-0008). Both
    # default to the pre-migration store: an environment that does not set
    # these keeps today's behaviour exactly.
    vector_backend: VectorBackend = "qdrant"
    lexical_backend: LexicalBackend = "tantivy"

    # --- vector store ---------------------------------------------------
    # Empty -> embedded mode under data_dir/qdrant. A URL selects Qdrant Server.
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "kb_chunks"
    qdrant_timeout: float = 30.0

    # --- lexical store (tantivy inverted index) --------------------------
    # Empty -> data_dir/lexical. Writers need a heap; tantivy's floor is 15 MB.
    lexical_dir: Path | None = None
    lexical_writer_heap_mb: int = 64
    # 0 lets tantivy pick. Multiple indexing threads create and replace segment
    # files concurrently, which on Windows can collide with an on-access virus
    # scanner and kill the writer with PermissionDenied on a .pos/.fieldnorm
    # file. Serialising costs ~2x on a full rebuild and nothing on incremental
    # ingest, so it is the safe default; raise it on Linux or with an exclusion.
    lexical_writer_threads: int = 1

    # --- embedding ------------------------------------------------------
    dense_provider: DenseProvider = "hash"
    dense_dim: int = 384
    dense_model: str = "BAAI/bge-small-zh-v1.5"
    dense_batch_size: int = 32
    # fastembed otherwise caches ONNX weights under the OS temp dir, where a
    # cleanup can silently delete them. Keep models with the rest of the data.
    model_cache_dir: Path | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""

    # --- text normalization ---------------------------------------------
    # Fold traditional and old glyph forms before indexing and before
    # embedding, so 陰陽 and 阴阳 retrieve the same passages. Applied to the
    # retrieval representation only - stored text is never rewritten.
    normalize_cjk: bool = True

    # --- chunking -------------------------------------------------------
    chunk_target_tokens: int = 512
    chunk_overlap_tokens: int = 64
    chunk_min_chars: int = 80
    chunk_max_chars: int = 4000

    # --- parsing --------------------------------------------------------
    parser_chain: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["docling", "unstructured", "marker"])

    # --- retrieval ------------------------------------------------------
    retrieval_overfetch: int = 4
    rrf_k: int = 60
    dense_weight: float = 1.0
    sparse_weight: float = 1.0
    reranker: Reranker = "lexical"
    rerank_model: str = "BAAI/bge-reranker-base"
    snippet_chars: int = 320

    # --- ingestion worker ----------------------------------------------
    worker_poll_interval: float = 1.0
    worker_lease_seconds: int = 300
    worker_max_attempts: int = 3
    worker_backoff_base: float = 5.0
    worker_backoff_cap: float = 600.0
    # None derives the safe default: enabled only for local + embedded mode.
    # Server deployments keep their independently scalable worker processes.
    api_worker_enabled: bool | None = None
    api_worker_shutdown_timeout: float = 30.0

    # --- api ------------------------------------------------------------
    auth_required: bool = False
    max_upload_bytes: int = 200 * 1024 * 1024
    allowed_mimes: Annotated[list[str], NoDecode] = Field(default_factory=list)  # empty -> allow all
    api_host: str = "127.0.0.1"
    api_port: int = 8077
    # Comma-separated list of root directories the /v1/ingest/path endpoint may
    # read from. Empty (default) allows any path -- safe for local dev, but
    # should be set in production to prevent arbitrary file reads.
    ingest_allowed_roots: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Global rate limit per client IP (requests per minute). 0 disables.
    rate_limit_per_minute: int = 0

    # --- plagiarism (PostgreSQL-only, default-off - see ADR-0001) --------
    # Two independent switches on purpose: indexing can run and backfill the
    # historical corpus while the public interface stays shut.
    plag_enabled: bool = False
    plag_indexing_enabled: bool = False

    # Algorithm. Changing any of these invalidates every stored projection -
    # they feed `plagiarism_algorithm_config_hash`, and a check may only run
    # against projections built with a matching hash.
    plag_sentences_per_chunk: int = 4
    plag_chunk_overlap: int = 1
    plag_kgram: int = 5
    plag_winnow_window: int = 8
    plag_min_seed_len: int = 30
    plag_min_passage_len: int = 50
    plag_zh_min_seed_len: int = 12
    plag_zh_min_passage_len: int = 20
    plag_min_effective_chars: int = 12
    plag_short_exact_min_score: float = 0.99
    # Bump together with plagiarism.normalization.NORMALIZER_VERSION when the
    # stored fingerprint representation changes.
    plag_normalizer_version: str = "plag-normalizer-v2"
    plag_extend_tolerance: float = 0.85
    plag_df_ratio_threshold: float = 0.25
    # A query chunk below this fraction of word characters is skipped: rule
    # lines and separator runs match each other exactly and mean nothing.
    plag_min_word_ratio: float = 0.5
    plag_candidate_top_k: int = 50

    # Jobs.
    plag_poll_interval: float = 1.0
    plag_lease_seconds: int = 600
    plag_max_attempts: int = 3
    plag_backoff_base: float = 5.0
    plag_backoff_cap: float = 600.0
    plag_worker_concurrency: int = 1

    # Limits.
    plag_max_input_chars: int = 500_000
    plag_budget_reference_chars: int = 100_000
    plag_budget_seconds: float = 60.0
    plag_max_active_checks_per_key: int = 2
    plag_preview_chars: int = 300

    # SSE.
    plag_sse_poll_interval: float = 0.5
    plag_sse_keepalive_interval: float = 2.0
    # Ceiling on one subscription. A client that wants more reconnects with
    # Last-Event-ID and loses nothing; an unbounded stream would pin a worker
    # thread on a check that never terminates.
    plag_sse_max_seconds: float = 900.0

    # Retention (days) for submitted text, reports, events and retired
    # projections.
    plag_retention_days: int = 30

    @field_validator("parser_chain", "allowed_mimes", "ingest_allowed_roots", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _backends_are_reachable_from_this_deployment(self) -> Settings:
        """Reject a switch this deployment cannot serve, at startup.

        Both mismatches are otherwise invisible until the first query, by which
        point the process is already accepting traffic.
        """
        for switch, backend in (
            ("vector_backend", self.vector_backend),
            ("lexical_backend", self.lexical_backend),
        ):
            if backend in _SQLITE_EMBEDDED_BACKENDS and self.profile != "local":
                raise ValueError(
                    f"{switch}={backend!r} lives inside the local SQLite file and needs "
                    f"profile=local, got profile={self.profile!r}"
                )
            if backend in _POSTGRES_BACKENDS and not _is_postgres_url(self.database_url):
                raise ValueError(
                    f"{switch}={backend!r} lives in the primary database and needs "
                    f"database_url to point at PostgreSQL, got {self.database_url!r}"
                )
        return self

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'kbsvc.db').as_posix()}"

    @property
    def resolved_storage_root(self) -> Path:
        return self.storage_root or (self.data_dir / "objects")

    @property
    def resolved_model_cache_dir(self) -> Path:
        return self.model_cache_dir or (self.data_dir / "models")

    @property
    def qdrant_local_path(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def resolved_lexical_dir(self) -> Path:
        return self.lexical_dir or (self.data_dir / "lexical")

    @property
    def use_embedded_qdrant(self) -> bool:
        return not self.qdrant_url

    @property
    def vector_store_is_in_process(self) -> bool:
        if self.vector_backend == "qdrant":
            return self.use_embedded_qdrant
        return self.vector_backend in _IN_PROCESS_BACKENDS

    @property
    def lexical_store_is_in_process(self) -> bool:
        return self.lexical_backend in _IN_PROCESS_BACKENDS

    @property
    def run_api_worker(self) -> bool:
        """Whether the API process also runs the ingest worker.

        Derived from store ownership, and deliberately conservative: the API
        takes the worker in only when it owns *every* store, because a store
        opened in-process is locked to that process and a separate worker
        could not write it. A mixed deployment - one store in-process, one
        over the network - keeps the worker external and leaves the in-process
        store to whichever process the operator points at it. That is already
        today's behaviour for `local` + `KB_QDRANT_URL`, where tantivy is
        in-process and the worker still runs outside the API.
        """
        if self.api_worker_enabled is not None:
            return self.api_worker_enabled
        return (
            self.profile == "local"
            and self.vector_store_is_in_process
            and self.lexical_store_is_in_process
        )

    @field_validator("plag_chunk_overlap")
    @classmethod
    def _overlap_must_let_the_window_advance(cls, value: int, info) -> int:
        per_chunk = info.data.get("plag_sentences_per_chunk")
        if per_chunk is not None and value >= per_chunk:
            raise ValueError(
                f"plag_chunk_overlap ({value}) must be less than "
                f"plag_sentences_per_chunk ({per_chunk}); otherwise the sliding "
                f"window cannot advance"
            )
        return value

    @field_validator(
        "plag_extend_tolerance", "plag_df_ratio_threshold", "plag_short_exact_min_score"
    )
    @classmethod
    def _must_be_a_ratio(cls, value: float) -> float:
        if not 0.0 < value <= 1.0:
            raise ValueError(f"must be in (0.0, 1.0], got {value}")
        return value

    @field_validator(
        "plag_sentences_per_chunk",
        "plag_kgram",
        "plag_winnow_window",
        "plag_min_seed_len",
        "plag_min_passage_len",
        "plag_zh_min_seed_len",
        "plag_zh_min_passage_len",
        "plag_min_effective_chars",
        "plag_candidate_top_k",
        "plag_max_input_chars",
        "plag_budget_reference_chars",
        "plag_max_active_checks_per_key",
        "plag_preview_chars",
        "plag_lease_seconds",
        "plag_max_attempts",
        "plag_worker_concurrency",
        "plag_retention_days",
    )
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(f"must be positive, got {value}")
        return value

    @model_validator(mode="after")
    def _plag_thresholds_are_ordered(self) -> Settings:
        if self.plag_zh_min_seed_len < self.plag_min_effective_chars:
            raise ValueError("plag_zh_min_seed_len must be >= plag_min_effective_chars")
        if self.plag_zh_min_passage_len <= self.plag_zh_min_seed_len:
            raise ValueError("plag_zh_min_passage_len must be greater than plag_zh_min_seed_len")
        return self

    @property
    def plagiarism_algorithm_config_hash(self) -> str:
        """Identifies the projection format a check may run against.

        Only parameters that change what gets *stored* belong here. Retrieval
        and alignment tuning (`min_seed_len`, `candidate_top_k`, ...) is applied
        at query time against an unchanged projection, so including it would
        force a full corpus rebuild for a change that needs none.

        Changing any contributing value invalidates every stored projection:
        new checks are refused until a rebuild republishes the corpus under the
        new hash.
        """
        payload = "|".join(
            str(part)
            for part in (
                "v2",
                self.plag_sentences_per_chunk,
                self.plag_chunk_overlap,
                self.plag_kgram,
                self.plag_winnow_window,
                self.plag_normalizer_version,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that mutate the environment."""
    get_settings.cache_clear()

"""Runtime configuration. Everything is driven by KB_* environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Profile = Literal["local", "server"]
DenseProvider = Literal["hash", "fastembed", "openai"]
Reranker = Literal["none", "lexical", "cross-encoder"]

DEFAULT_DATA_DIR = Path.cwd() / ".kbdata"


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

    # --- vector store ---------------------------------------------------
    # Empty -> embedded mode under data_dir/qdrant. A URL selects Qdrant Server.
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "kb_chunks"
    qdrant_timeout: float = 30.0

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

    # --- chunking -------------------------------------------------------
    chunk_target_tokens: int = 512
    chunk_overlap_tokens: int = 64
    chunk_min_chars: int = 80
    chunk_max_chars: int = 4000

    # --- parsing --------------------------------------------------------
    parser_chain: list[str] = Field(default_factory=lambda: ["docling", "unstructured", "marker"])

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
    allowed_mimes: list[str] = Field(default_factory=list)  # empty -> allow all
    api_host: str = "127.0.0.1"
    api_port: int = 8077

    @field_validator("parser_chain", "allowed_mimes", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

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
    def use_embedded_qdrant(self) -> bool:
        return not self.qdrant_url

    @property
    def run_api_worker(self) -> bool:
        if self.api_worker_enabled is not None:
            return self.api_worker_enabled
        return self.profile == "local" and self.use_embedded_qdrant


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that mutate the environment."""
    get_settings.cache_clear()

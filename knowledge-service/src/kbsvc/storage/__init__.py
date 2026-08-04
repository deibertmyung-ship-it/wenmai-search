"""Object store factory."""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings
from .base import ObjectStore
from .local import LocalObjectStore


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    settings = get_settings()
    if settings.storage_backend == "s3":
        from .s3 import S3ObjectStore

        return S3ObjectStore(settings)
    return LocalObjectStore(settings.resolved_storage_root)


def reset_object_store_cache() -> None:
    get_object_store.cache_clear()


__all__ = ["ObjectStore", "get_object_store", "reset_object_store_cache"]

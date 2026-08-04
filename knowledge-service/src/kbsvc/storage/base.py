"""Object storage abstraction: the original bytes always survive re-parsing."""

from __future__ import annotations

from typing import Protocol


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "") -> str:
        """Store bytes under key, return a URI that identifies the object."""

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def uri(self, key: str) -> str: ...

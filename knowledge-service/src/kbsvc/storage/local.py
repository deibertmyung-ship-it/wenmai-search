"""Filesystem-backed object store (local profile)."""

from __future__ import annotations

from pathlib import Path

from ..errors import NotFoundError, ValidationError


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValidationError("object key escapes storage root", {"key": key})
        return candidate

    def put(self, key: str, data: bytes, *, content_type: str = "") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.uri(key)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise NotFoundError("object not found", {"key": key})
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()

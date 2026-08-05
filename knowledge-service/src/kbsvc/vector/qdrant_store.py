"""Qdrant-backed vector store.

Runs identically in embedded mode (KB_QDRANT_URL empty -> on-disk local client)
and against a Qdrant server.

This store owns the dense half of retrieval only. The lexical half moved to
`kbsvc.lexical`, a tantivy inverted index: the embedded Qdrant client has no
sparse index and scored every chunk in Python. Fusion still happens in kbsvc,
which keeps both profiles behaviourally identical and keeps per-retriever debug
output available.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import shutil
import time
from collections.abc import Iterator
from threading import RLock

from qdrant_client import QdrantClient, models

from ..config import Settings, get_settings
from ..errors import KbError
from .base import SearchFilter, SearchHit, VectorPoint

logger = logging.getLogger(__name__)

DENSE_VECTOR = "dense"

_PURGE_ATTEMPTS = 5
_PURGE_BACKOFF = 0.4

_PAYLOAD_KEYWORD_INDEXES = ("tenant_id", "document_id", "version_id", "source_id", "acl", "kind")


class QdrantVectorStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.collection = self.settings.qdrant_collection
        self._local_lock = RLock() if self.settings.use_embedded_qdrant else None
        if self.settings.use_embedded_qdrant:
            path = self.settings.qdrant_local_path
            path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(
                path=str(path),
                force_disable_check_same_thread=True,
            )
        else:
            self.client = QdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key or None,
                timeout=int(self.settings.qdrant_timeout),
            )
        self._ready = False

    @contextlib.contextmanager
    def _client_access(self) -> Iterator[None]:
        """Serialize embedded operations shared by API and worker threads."""
        if self._local_lock is None:
            yield
            return
        with self._local_lock:
            yield

    # --- lifecycle ------------------------------------------------------

    def ensure_collection(self, dim: int) -> None:
        with self._client_access():
            if self._ready:
                return
            if not self.client.collection_exists(self.collection):
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config={
                        DENSE_VECTOR: models.VectorParams(
                            size=dim, distance=models.Distance.COSINE
                        )
                    },
                )
                # Embedded Qdrant scans payloads directly; indexes matter on a server.
                if not self.settings.use_embedded_qdrant:
                    for field in _PAYLOAD_KEYWORD_INDEXES:
                        self._create_index(field, models.PayloadSchemaType.KEYWORD)
                    self._create_index("is_current", models.PayloadSchemaType.BOOL)
            self._ready = True

    def recreate_collection(self, dim: int) -> None:
        """Drop and rebuild the collection.

        Required whenever the dense model changes: vector dimension and vector
        space are both baked into the collection at creation time.
        """
        with self._client_access():
            if self.client.collection_exists(self.collection):
                self.client.delete_collection(self.collection)
            self._ready = False

            if self.settings.use_embedded_qdrant:
                self._purge_local_collection()

            self.ensure_collection(dim)

    def _purge_local_collection(self) -> None:
        """Physically remove an embedded collection and reopen the client.

        In embedded mode `delete_collection` updates the collection config but
        leaves the persisted vector storage in place. Re-creating with a
        different dimension then fails at upsert with a numpy broadcast error,
        because the old fixed-width array is still there. Deleting the directory
        and reconnecting is the only reliable reset.
        """
        path = self.settings.qdrant_local_path
        target = path / "collection" / self.collection

        with contextlib.suppress(Exception):
            self.client.close()

        # Windows releases the sqlite handle lazily, so retry briefly. Never
        # swallow the failure: a half-purged collection would silently keep the
        # old vector width and blow up later at upsert time.
        last_error: Exception | None = None
        for attempt in range(_PURGE_ATTEMPTS):
            if not target.exists():
                last_error = None
                break
            try:
                shutil.rmtree(target)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                gc.collect()
                time.sleep(_PURGE_BACKOFF * (attempt + 1))

        if target.exists():
            raise KbError(
                "could not purge the embedded Qdrant collection; "
                "stop every process using this data directory and retry",
                {"path": str(target), "reason": str(last_error) if last_error else "still present"},
            )

        self.client = QdrantClient(
            path=str(path),
            force_disable_check_same_thread=True,
        )
        logger.info("purged embedded collection storage at %s", target)

    def _create_index(self, field: str, schema: models.PayloadSchemaType) -> None:
        try:
            self.client.create_payload_index(
                collection_name=self.collection, field_name=field, field_schema=schema
            )
        except Exception as exc:  # pragma: no cover - server-only path
            logger.warning("payload index %s not created: %s", field, exc)

    def close(self) -> None:
        with self._client_access(), contextlib.suppress(Exception):
            self.client.close()

    # --- writes ---------------------------------------------------------

    def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        with self._client_access():
            self.client.upsert(
                collection_name=self.collection,
                points=[
                    models.PointStruct(
                        id=point.id,
                        vector={DENSE_VECTOR: point.dense},
                        payload=point.payload,
                    )
                    for point in points
                ],
                wait=True,
            )

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._client_access():
            self.client.delete(
                collection_name=self.collection,
                points_selector=models.PointIdsList(points=ids),
                wait=True,
            )

    def delete_by_document(self, tenant_id: str, document_id: str) -> None:
        self._delete_where(
            [
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                ),
            ]
        )

    def delete_by_versions(self, tenant_id: str, version_ids: list[str]) -> None:
        if not version_ids:
            return
        self._delete_where(
            [
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                models.FieldCondition(key="version_id", match=models.MatchAny(any=version_ids)),
            ]
        )

    def _delete_where(self, conditions: list) -> None:
        with self._client_access():
            if not self.client.collection_exists(self.collection):
                return
            self.client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(filter=models.Filter(must=conditions)),
                wait=True,
            )

    # --- reads ----------------------------------------------------------

    def _build_filter(self, flt: SearchFilter) -> models.Filter:
        must: list = [
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=flt.tenant_id))
        ]
        if flt.current_only:
            must.append(
                models.FieldCondition(key="is_current", match=models.MatchValue(value=True))
            )
        if flt.acl_any:
            must.append(models.FieldCondition(key="acl", match=models.MatchAny(any=flt.acl_any)))
        if flt.source_ids:
            must.append(
                models.FieldCondition(key="source_id", match=models.MatchAny(any=flt.source_ids))
            )
        if flt.document_ids:
            must.append(
                models.FieldCondition(
                    key="document_id", match=models.MatchAny(any=flt.document_ids)
                )
            )
        if flt.kinds:
            must.append(models.FieldCondition(key="kind", match=models.MatchAny(any=flt.kinds)))
        return models.Filter(must=must)

    def _query(self, query, using: str, limit: int, flt: SearchFilter) -> list[SearchHit]:
        with self._client_access():
            if not self.client.collection_exists(self.collection):
                return []
            response = self.client.query_points(
                collection_name=self.collection,
                query=query,
                using=using,
                limit=limit,
                query_filter=self._build_filter(flt),
                with_payload=True,
            )
            return [
                SearchHit(id=str(point.id), score=float(point.score), payload=point.payload or {})
                for point in response.points
            ]

    def search_dense(
        self, vector: list[float], *, limit: int, flt: SearchFilter
    ) -> list[SearchHit]:
        return self._query(vector, DENSE_VECTOR, limit, flt)

    def count(self, tenant_id: str | None = None) -> int:
        with self._client_access():
            if not self.client.collection_exists(self.collection):
                return 0
            count_filter = None
            if tenant_id:
                count_filter = models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id", match=models.MatchValue(value=tenant_id)
                        )
                    ]
                )
            return self.client.count(
                collection_name=self.collection, count_filter=count_filter, exact=True
            ).count

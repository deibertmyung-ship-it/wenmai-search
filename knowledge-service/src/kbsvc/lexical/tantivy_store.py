"""Tantivy-backed lexical index.

Replaces a hand-rolled BM25 that was encoded into Qdrant sparse vectors. That
arrangement worked, but the embedded Qdrant client has no sparse index and
scored every chunk in Python: ~5.6s per query on a 22.6k-chunk corpus. Tantivy
is a real inverted index and answers the same queries in ~1ms.

The corpus is analysed by `tokenizer.analyze` before it reaches tantivy, so
tantivy's own analyzer only splits on whitespace. That keeps the CJK
unigram+bigram scheme - which is domain knowledge worth preserving - while
handing scoring, storage and deletion to a real engine.

Concurrency: one writer per process, guarded by a lock, because the API request
threads and the API-owned ingest worker thread share this object. Tantivy also
takes a directory lock, so the CLI must not write while the API is running -
`run.bat reembed` already stops the stack first.

Across processes (the server profile, where the writer is the worker container
and the API and MCP containers only read a shared index directory) readers must
reload to see published commits; see `_refresh_reader`. That directory lock also
caps the deployment at exactly one writing worker process.
"""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from threading import RLock

import tantivy

from ..config import Settings, get_settings
from ..errors import KbError
from .base import LexicalDocument, SearchFilter, SearchHit
from .tokenizer import analyze, tokenize

logger = logging.getLogger(__name__)

ANALYZER = "kbgram"

_PURGE_ATTEMPTS = 5
_PURGE_BACKOFF = 0.4
_COMMIT_ATTEMPTS = 3
_COMMIT_BACKOFF = 0.3
# How often a read-only process re-reads the segment metadata. Bounds the cost
# for a busy API while keeping newly indexed documents visible within a second.
_READER_RELOAD_INTERVAL = 1.0

_TEXT_FIELDS = ("chunk_id", "tenant_id", "document_id", "version_id", "source_id", "kind", "acl")


def _build_schema() -> tantivy.Schema:
    builder = tantivy.SchemaBuilder()
    # `raw` keeps the whole value as one term: exact-match filters, and the
    # term that `delete_documents_by_term` needs.
    for name in _TEXT_FIELDS:
        builder.add_text_field(
            name, stored=(name == "chunk_id"), tokenizer_name="raw", index_option="basic"
        )
    builder.add_boolean_field("is_current", stored=False, indexed=True)
    # The scored field. `freq` rather than `position` because nothing here
    # phrase-queries, and dropping positions makes the index materially smaller.
    builder.add_text_field("body", stored=False, tokenizer_name=ANALYZER, index_option="freq")
    # The payload travels with the hit so a sparse-only search can render
    # results without a second round trip to another store.
    builder.add_text_field("payload", stored=True, tokenizer_name="raw", index_option="basic")
    return builder.build()


class TantivyLexicalStore:
    name = "tantivy"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = self.settings.resolved_lexical_dir
        self._lock = RLock()
        self._index: tantivy.Index | None = None
        self._writer: tantivy.IndexWriter | None = None
        self._bulk = False
        self._last_reload = 0.0

    # --- lifecycle ------------------------------------------------------

    def ensure_ready(self) -> None:
        with self._lock:
            self._open()

    def _open(self) -> tantivy.Index:
        if self._index is not None:
            return self._index
        self.path.mkdir(parents=True, exist_ok=True)
        schema = _build_schema()
        if tantivy.Index.exists(str(self.path)):
            index = tantivy.Index.open(str(self.path))
        else:
            index = tantivy.Index(schema, path=str(self.path))
        index.register_tokenizer(
            ANALYZER, tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.whitespace()).build()
        )
        self._index = index
        return index

    def _get_writer(self) -> tantivy.IndexWriter:
        if self._writer is None:
            index = self._open()
            heap = self.settings.lexical_writer_heap_mb * 1024 * 1024
            self._writer = index.writer(
                heap_size=heap, num_threads=self.settings.lexical_writer_threads
            )
        return self._writer

    def _refresh_reader(self, index: tantivy.Index) -> None:
        """Pick up commits published by a writer in another process.

        A process that writes already reloads in `_commit`, so this is skipped
        there - that is the local profile, where the API owns both the request
        threads and the ingest worker thread. In the server profile the writer
        lives in the worker container while the API and MCP containers only
        read the shared index directory; without this they would keep answering
        from the segment set they happened to see at startup, and a freshly
        ingested document would never appear in sparse results.

        Throttled: a reload re-reads segment metadata, which is wasted work
        several times per second on a busy API.
        """
        if self._writer is not None:
            return
        now = time.monotonic()
        if now - self._last_reload < _READER_RELOAD_INTERVAL:
            return
        index.reload()
        self._last_reload = now

    def _commit(self) -> None:
        """Publish pending writes and make them visible to new searchers.

        Deliberately does NOT wait on merge threads. `wait_merging_threads`
        consumes the writer, so calling it per batch means building a new one
        each time - and on Windows the next writer can open before the previous
        merge has released its file handles, which surfaces as a PermissionDenied
        on a .fieldnorm file. Merges are waited on once, at close().
        """
        if self._writer is None or self._bulk:
            return
        # A commit creates and replaces segment files. Under disk pressure an
        # on-access scanner can still be holding one of them, which surfaces as
        # PermissionDenied on a .term/.pos/.fieldnorm file. That is transient:
        # retry briefly rather than failing an ingest job over it. If the writer
        # itself was killed the retries will not help, and the error propagates -
        # which is correct, because the batch genuinely did not land.
        last_error: Exception | None = None
        for attempt in range(_COMMIT_ATTEMPTS):
            try:
                self._writer.commit()
                if self._index is not None:
                    self._index.reload()
                return
            except (OSError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "lexical commit failed (attempt %d/%d): %s",
                    attempt + 1,
                    _COMMIT_ATTEMPTS,
                    exc,
                )
                time.sleep(_COMMIT_BACKOFF * (attempt + 1))
        raise KbError(f"lexical commit failed after {_COMMIT_ATTEMPTS} attempts: {last_error}")

    @contextlib.contextmanager
    def bulk(self) -> Iterator[None]:
        """Load many documents with a single commit at the end.

        A full rebuild writes tens of thousands of documents. Committing per
        batch makes tantivy merge segments while later batches are still being
        written, and on Windows those merge threads and the writer race for the
        same segment files - which surfaces as PermissionDenied on a .pos or
        .fieldnorm file and kills the writer. One commit means one merge pass,
        after everything is in.

        Incremental ingest deliberately does NOT use this: there, a document
        should become searchable as soon as its job completes.
        """
        with self._lock:
            self._bulk = True
            try:
                yield
            finally:
                self._bulk = False
                self._commit()

    def recreate(self) -> None:
        """Empty the index. The migration and repair path.

        Prefers tantivy's own `delete_all_documents` over deleting the directory:
        the schema is unchanged, so there is nothing the filesystem needs to
        rebuild, and on Windows an rmtree here races with handles the OS has not
        released yet. Falls back to a physical purge only when the index cannot
        be opened at all (corrupt, or written by an older schema).
        """
        with self._lock:
            try:
                writer = self._get_writer()
                writer.delete_all_documents()
                writer.commit()
                writer.garbage_collect_files()
                if self._index is not None:
                    self._index.reload()
                return
            except Exception as exc:
                logger.warning("lexical soft-reset failed (%s); purging the directory", exc)
            self.close()
            self._purge()
            self._open()

    def _purge(self) -> None:
        """Physically remove the index directory.

        Windows releases tantivy's mmap handles lazily, so a fresh index created
        in the same path can inherit a half-deleted directory and then die with
        PermissionDenied on a .pos/.fieldnorm file mid-write. `close()` has
        already dropped the references and collected; retry briefly for the
        handles the OS has not released yet. Never swallow the final failure -
        building on top of a partially deleted index corrupts silently.
        """
        last_error: Exception | None = None
        for attempt in range(_PURGE_ATTEMPTS):
            if not self.path.exists():
                return
            try:
                shutil.rmtree(self.path)
                return
            except OSError as exc:
                last_error = exc
                gc.collect()
                time.sleep(_PURGE_BACKOFF * (attempt + 1))
        if last_error is not None:
            raise KbError(f"could not clear the lexical index at {self.path}: {last_error}")

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                try:
                    self._writer.commit()
                    self._writer.wait_merging_threads()
                except Exception as exc:  # pragma: no cover - teardown best effort
                    logger.warning("lexical writer close failed: %s", exc)
                self._writer = None
            self._index = None
            # Dropping the Python reference is not enough: the Rust Index keeps
            # its mmaps until the object is actually collected, and on Windows
            # those handles block deletion of the very files we are about to
            # rewrite. Same reason the Qdrant store forces this on purge.
            gc.collect()

    # --- writes ---------------------------------------------------------

    def upsert(self, documents: list[LexicalDocument]) -> None:
        if not documents:
            return
        with self._lock:
            writer = self._get_writer()
            for item in documents:
                # Tantivy has no update: delete the old term first, then add.
                # A bulk load starts from an empty index, so there is nothing to
                # replace and the delete would only add opstamps to churn on.
                if not self._bulk:
                    writer.delete_documents_by_term("chunk_id", item.id)
                writer.add_document(_to_document(item))
            self._commit()

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            writer = self._get_writer()
            for chunk_id in ids:
                writer.delete_documents_by_term("chunk_id", chunk_id)
            self._commit()

    def delete_by_document(self, tenant_id: str, document_id: str) -> None:
        with self._lock:
            self._get_writer().delete_documents_by_term("document_id", document_id)
            self._commit()

    def delete_by_versions(self, tenant_id: str, version_ids: list[str]) -> None:
        if not version_ids:
            return
        with self._lock:
            writer = self._get_writer()
            for version_id in version_ids:
                writer.delete_documents_by_term("version_id", version_id)
            self._commit()

    # --- reads ----------------------------------------------------------

    def search(self, query: str, *, limit: int, flt: SearchFilter) -> list[SearchHit]:
        terms = sorted(set(tokenize(query)))
        if not terms:
            return []
        with self._lock:
            index = self._open()
            self._refresh_reader(index)
            searcher = index.searcher()
            schema = index.schema
        body = tantivy.Query.boolean_query(
            [
                (tantivy.Occur.Should, tantivy.Query.term_query(schema, "body", term))
                for term in terms
            ]
        )
        clauses: list[tuple] = [(tantivy.Occur.Must, body)]
        clauses.append(
            (tantivy.Occur.Must, tantivy.Query.term_query(schema, "tenant_id", flt.tenant_id))
        )
        if flt.current_only:
            clauses.append(
                (tantivy.Occur.Must, tantivy.Query.term_query(schema, "is_current", True))
            )
        for field, values in (
            ("acl", flt.acl_any),
            ("source_id", flt.source_ids),
            ("document_id", flt.document_ids),
            ("kind", flt.kinds),
        ):
            if values:
                clauses.append((tantivy.Occur.Must, _any_of(schema, field, values)))

        hits = searcher.search(tantivy.Query.boolean_query(clauses), limit).hits
        results: list[SearchHit] = []
        for score, address in hits:
            doc = searcher.doc(address)
            raw = doc.get_first("payload")
            results.append(
                SearchHit(
                    id=doc.get_first("chunk_id"),
                    score=float(score),
                    payload=json.loads(raw) if raw else {},
                )
            )
        return results

    def count(self, tenant_id: str | None = None) -> int:
        with self._lock:
            index = self._open()
            index.reload()
            searcher = index.searcher()
            schema = index.schema
        if tenant_id is None:
            return searcher.num_docs
        query = tantivy.Query.term_query(schema, "tenant_id", tenant_id)
        return searcher.search(query, 1).count or 0


def _any_of(schema: tantivy.Schema, field: str, values: list[str]) -> tantivy.Query:
    return tantivy.Query.boolean_query(
        [(tantivy.Occur.Should, tantivy.Query.term_query(schema, field, value)) for value in values]
    )


def _to_document(item: LexicalDocument) -> tantivy.Document:
    payload = item.payload
    doc = tantivy.Document()
    doc.add_text("chunk_id", item.id)
    doc.add_text("tenant_id", str(payload.get("tenant_id", "")))
    doc.add_text("document_id", str(payload.get("document_id", "")))
    doc.add_text("version_id", str(payload.get("version_id", "")))
    doc.add_text("source_id", str(payload.get("source_id", "")))
    doc.add_text("kind", str(payload.get("kind", "text")))
    for tag in payload.get("acl") or ["public"]:
        doc.add_text("acl", str(tag))
    doc.add_boolean("is_current", bool(payload.get("is_current", True)))
    doc.add_text("body", analyze(item.text))
    doc.add_text("payload", json.dumps(payload, ensure_ascii=False))
    return doc


def lexical_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).resolved_lexical_dir

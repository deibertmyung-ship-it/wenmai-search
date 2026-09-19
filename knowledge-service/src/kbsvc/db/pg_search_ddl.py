"""DDL helpers for pg_search-backed lexical retrieval (ADR-0008 ticket 07).

Like pgvector's dense-retrieval columns (``db/pgvector_ddl.py``), pg_search's
lexical-retrieval columns live directly on the `chunk` table the metadata
store already owns, via additive ``ALTER TABLE`` statements - not a separate
table the way sqlite-vec's `chunk_vec`/FTS5's `chunk_fts` are, since the
`server` profile's `chunk` already lives in the same PostgreSQL database.
This keeps a `SearchHit` a single-row read with no join, the same property
`vector_payload`/`chunk_vec_payload`/FTS5's `payload` column each give their
own store.

Two columns are genuinely new here:

- `body text` - pre-analysed, whitespace-joined text. `LexicalDocument.text`
  (`lexical/base.py`) carries *raw* chunk text; `PgSearchLexicalStore.upsert`
  calls `tokenizer.analyze()` before writing it, exactly as
  `TantivyLexicalStore`/`Fts5LexicalStore` do - the ticket's own DDL sketch
  names a `body` column that does not exist on `chunk` and assumes
  pre-analysed, whitespace-tokenized content (its
  `"body": {"tokenizer": {"type": "whitespace"}}` config only makes sense
  against text `analyze()` already split on token boundaries - a raw-text
  `body` fed to a whitespace tokenizer would lose the CJK unigram+bigram
  scheme entirely).
- `lexical_payload jsonb` - the denormalized JSON view `search()`'s caller
  needs to render a hit with no join (`retrieval/pipeline.py` reads
  `title`/`source_uri`/`heading_path`/`text`/... straight off
  `SearchHit.payload`). Mirrors `vector_payload`/`chunk_vec_payload`/FTS5's
  `payload` column: one independent copy per store, not shared, even though
  in the common case (both `pgvector` and `pg-search` active) it holds the
  same JSON `vector_payload` does - see the ownership note below for why it
  is not read from that column instead.

`source_id`/`acl`/`is_current` are NOT new - ticket 06's `PgVectorStore`
already adds them - but this module adds them again, idempotently.
`config.py`'s backend validator checks `vector_backend` and `lexical_backend`
independently, so nothing stops a deployment running `KB_LEXICAL_BACKEND=
pg-search` while `KB_VECTOR_BACKEND` stays `qdrant`/`tantivy` (or the
reverse) - in the "pg-search without pgvector" shape, `PgVectorStore` is
never constructed and never adds these columns, so this module's own
schema-ensure must be self-sufficient regardless of which store (or both, or
neither, in either order) runs first in a given process. Deliberately
duplicated rather than imported from `db/pgvector_ddl.py`: kept as literal,
identical SQL (same types and defaults - `text NOT NULL DEFAULT ''`,
`text[] NOT NULL DEFAULT '{}'`, `boolean NOT NULL DEFAULT true`) rather than
a shared import, because this ticket's own scope boundary excludes editing
`db/pgvector_ddl.py`. `ADD COLUMN IF NOT EXISTS` is idempotent, so both
modules issuing the same statements is harmless either way; keep the two
tuples byte-for-byte identical when editing either, so they cannot silently
drift apart.

Like sqlite-vec's `chunk_vec`/pgvector's `embedding`, the bm25 index has no
runtime-discovered dimension - unlike `embedding vector(N)`, nothing about
this schema depends on a value only known at runtime from the embedder - so
it *could* run inside `init_db()`. It stays lazy anyway, invoked from
`PgSearchLexicalStore.ensure_ready()`, for the same self-sufficiency property
`ensure_vec0_table`/`ensure_fts5_table` give their stores: a store built
directly against a fresh database, with no `init_db()` call first (as this
ticket's own test suite does, pointing at the live dev database without
assuming another process already ran `init_db()`), still ends up usable.

All six `SearchFilter` fields (`tenant_id`, `document_ids`, `source_ids`,
`kinds`, `acl_any`, `current_only`) are represented in the bm25 index's field
list (`tenant_id`, `document_id`, `source_id`, `kind`, `acl`, `is_current`) -
the ticket's own DDL sketch only had four of the six and flagged the gap
itself ("source_id 与 kind 也在 SearchFilter 里...补上").

Single transaction, unlike `pgvector_ddl.py`'s two-phase ALTER/CREATE INDEX
split: that split exists there so a `SET LOCAL maintenance_work_mem` tuning
statement stays scoped to the HNSW build alone. The bm25 index build has no
equivalent tuning need (ticket-measured: ~3s at 100k chunks with no special
settings), so there is nothing to isolate a second transaction for.
"""

from __future__ import annotations

import json

from sqlalchemy import Connection, Engine, text

from .session import get_engine

Bind = Engine | Connection

# This store's own columns - see the module docstring for why both are new
# and nullable (a chunk with no lexical data yet, or one whose lexical data
# was nulled by delete/recreate, is a perfectly valid state, mirroring
# `embedding`/`vector_payload`'s nullability on the pgvector side).
_ADD_LEXICAL_COLUMNS_SQL: tuple[str, ...] = (
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS body text",
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS lexical_payload jsonb",
)

# Duplicated from `db/pgvector_ddl.py`'s `_ADD_METADATA_COLUMNS_SQL` (minus
# `vector_payload`, which belongs solely to `PgVectorStore`) - see the module
# docstring's "genuinely new" section for why this is a deliberate literal
# duplication rather than a shared import.
_ADD_SHARED_METADATA_COLUMNS_SQL: tuple[str, ...] = (
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS source_id text NOT NULL DEFAULT ''",
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS acl text[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE chunk ADD COLUMN IF NOT EXISTS is_current boolean NOT NULL DEFAULT true",
)

# `raw` tokenizer + `fast` (a pg_search "fast field", stored columnar for
# cheap exact-match/filter evaluation - the DSL analogue of a btree index) on
# every filter field; `whitespace` on `body` because `analyze()` has already
# done the real tokenization (CJK unigram+bigram) and pg_search must not
# re-split it - identical reasoning to `TantivyLexicalStore`'s custom
# whitespace-only tokenizer and `Fts5LexicalStore`'s reliance on `unicode61`
# only ever seeing pre-analysed text. Built via `json.dumps` rather than a
# hand-escaped literal so the WITH clause's embedded JSON can never drift out
# of sync with a typo'd quote.
_TEXT_FIELDS_CONFIG = {
    # `record: "freq"` omits positions (this query path never issues phrase
    # queries) and is measurably faster than the default `"position"` while
    # keeping term-frequency BM25 scoring intact. Filter fields use `"basic"`
    # because they are exact-value matches with no ranking role.
    "body": {"tokenizer": {"type": "whitespace"}, "record": "freq"},
    "tenant_id": {"tokenizer": {"type": "raw"}, "fast": True, "record": "basic"},
    "document_id": {"tokenizer": {"type": "raw"}, "fast": True, "record": "basic"},
    "source_id": {"tokenizer": {"type": "raw"}, "fast": True, "record": "basic"},
    "kind": {"tokenizer": {"type": "raw"}, "fast": True, "record": "basic"},
    "acl": {"tokenizer": {"type": "raw"}, "fast": True, "record": "basic"},
}
_BOOLEAN_FIELDS_CONFIG = {"is_current": {"fast": True}}

_BM25_INDEX_NAME = "chunk_bm25"
_CREATE_BM25_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {_BM25_INDEX_NAME} ON chunk "
    "USING bm25 (id, body, tenant_id, document_id, source_id, kind, acl, is_current) "
    "WITH (key_field = 'id', target_segment_count = 1, "
    f"text_fields = '{json.dumps(_TEXT_FIELDS_CONFIG)}', "
    f"boolean_fields = '{json.dumps(_BOOLEAN_FIELDS_CONFIG)}')"
)


def ensure_pg_search_schema(bind: Engine | Connection | None = None) -> None:
    """Add the lexical-retrieval columns and the bm25 index to `chunk` if
    missing.

    Idempotent: every `ALTER TABLE`/`CREATE INDEX` is `IF NOT EXISTS`-guarded
    (confirmed against the live dev database, including `CREATE INDEX IF NOT
    EXISTS ... USING bm25` specifically - it is not obvious a non-builtin
    index access method honours the clause the same way btree/gin do, and it
    does), the same additive-only shape every other `ensure_*` helper in this
    package uses (ADR-0004 forbids a migration framework).

    *bind* may be a live ``Connection`` (ticket 08's publish transaction) or an
    ``Engine``.
    """
    if bind is None:
        bind = get_engine()
    if isinstance(bind, Connection):
        for stmt in _ADD_LEXICAL_COLUMNS_SQL:
            bind.execute(text(stmt))
        for stmt in _ADD_SHARED_METADATA_COLUMNS_SQL:
            bind.execute(text(stmt))
        bind.execute(text(_CREATE_BM25_INDEX_SQL))
        return
    with bind.begin() as conn:
        for stmt in _ADD_LEXICAL_COLUMNS_SQL:
            conn.execute(text(stmt))
        for stmt in _ADD_SHARED_METADATA_COLUMNS_SQL:
            conn.execute(text(stmt))
        conn.execute(text(_CREATE_BM25_INDEX_SQL))

"""A/B acceptance harness for the ADR-0008 storage consolidation.

This is the project's only retrieval-quality gate: there is no labelled
evaluation set (the gap ADR-0002/0003/0007 all point at), so the migration
instead proves equivalence by running the *same* queries against both the old
and new stacks and comparing top-k. The four cells and their assertion kinds
are fixed in code *before* any run - thresholds cannot be tuned to fit a
result after the fact:

|        | local                              | server                              |
|--------|------------------------------------|-------------------------------------|
| lexical| HARD top-k SET equality            | HARD top-k SET equality             |
| dense  | HARD top-k SET equality (brute)    | SOFT Recall@10(new) >= Recall@10(old)|

The hard lexical/dense cells compare id *sets*, not ordered sequences: FTS5
and Tantivy break ties differently, so a sequence assertion would false-fire
on equal relevance. Queries whose rarest-or-most-frequent term has document
frequency at or above `HIGH_DF_RATIO` (0.5, measured in spike_05_idf.py) land
in an *expected-divergence* bucket for the lexical cells: FTS5 clamps that
term's IDF to 1e-06 where Tantivy keeps it ~0.11, so the two ranks differ
by construction. That bucket is reported separately and does not fail the
gate. Below the threshold the two engines agree to ~1%.

The server dense cell is soft because both sides are approximate (Qdrant
HNSW vs pgvector HNSW): demanding set equality would require the new index
to reproduce the old index's approximation error. Ground truth is the exact
brute-force result (`SET enable_indexscan = off` on pgvector), and the gate
only requires the new stack's recall not to regress below the old one's.

Query synthesis is reproducible (fixed seed) because there are no query logs
in the corpus - random 8-20-char slices of chunk text give coverage, not
realism, which is sufficient for a regression gate. Each query carries the
maximum document frequency of its terms so the report can stratify.
"""

from __future__ import annotations

import contextlib
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select

from ..db.models import Chunk, Document
from ..db.session import session_scope
from ..lexical.tokenizer import tokenize
from ..vector.base import SearchFilter

logger = logging.getLogger(__name__)

# Document-frequency ratio at/above which a lexical query is expected to
# diverge between FTS5 and Tantivy. spike_05_idf.py measured the IDF clamp at
# df >= 50%; it is a real engine difference, not a migration bug.
HIGH_DF_RATIO = 0.5

# Top-K used by every cell. 10 matches the serving default and is the k the
# soft recall cell is named for (Recall@10).
TOP_K = 10

# A dense retriever answers a query vector with the top-k hit ids it produces.
DenseRetriever = Callable[[list[float], SearchFilter], list[str]]
# A lexical retriever answers query text the same way.
LexicalRetriever = Callable[[str, SearchFilter], list[str]]


@dataclass(frozen=True)
class SampleQuery:
    """One synthetic query plus the filter it runs under."""

    text: str
    flt: SearchFilter
    # Why the query exists: "random" | "function_word" | "single_char" |
    # "empty_result" | "filtered_narrow" | "filtered_wide".
    category: str
    # Filter selectivity tier for the server-dense stratification:
    # "none" | "narrow" | "wide".
    filter_tier: str = "none"
    # Filled in after df is counted: max df ratio among the query's terms.
    max_df_ratio: float = 0.0


@dataclass
class CellResult:
    """Outcome of one of the four cells."""

    name: str
    kind: str  # "hard" | "soft"
    total: int = 0
    passed: int = 0
    # Queries in the expected-divergence bucket (lexical high-df): counted
    # and reported, but never failed.
    expected_divergence: int = 0
    # For the soft cell, mean recall per stack and the number that regressed.
    recall_old: float | None = None
    recall_new: float | None = None
    regressions: int = 0
    # Stratified counters: key -> (total, passed) for hard, key -> recall
    # pair for soft.
    strata: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        if self.kind == "hard":
            # Every non-expected-divergence query must pass.
            return self.passed == self.total - self.expected_divergence
        return self.regressions == 0


# ---------------------------------------------------------------------------
# Query synthesis + document frequency
# ---------------------------------------------------------------------------


def generate_queries(
    tenant_id: str,
    *,
    count: int = 1000,
    seed: int = 42,
) -> list[SampleQuery]:
    """Build a reproducible synthetic query set from stored chunk text.

    Random 8-20-char slices give broad coverage; curated queries force the
    edge cases the ticket lists (high-frequency function words, single
    characters, empty results, and filtered variants). Document/source ids
    for the filtered queries are sampled from real rows so the filters are
    selective rather than no-ops.
    """
    rng = random.Random(seed)
    queries: list[SampleQuery] = []

    with session_scope() as session:
        rows = list(
            session.execute(
                select(Chunk.text, Document.id, Document.source_id)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.tenant_id == tenant_id)
            )
        )
        if not rows:
            return []

        texts = [r[0] for r in rows]
        doc_ids = list({r[1] for r in rows})
        source_ids = list({r[2] for r in rows if r[2]})

        def _slice() -> str:
            text = rng.choice(texts)
            # Strip whitespace so a slice that happens to start/end on a
            # separator does not tokenize to nothing.
            text = "".join(text.split())
            if len(text) <= 8:
                return text
            length = rng.randint(8, min(20, len(text)))
            start = rng.randint(0, len(text) - length)
            return text[start : start + length]

        # Bulk of the set is random slices.
        for _ in range(max(0, count - 20)):
            queries.append(
                SampleQuery(text=_slice(), flt=SearchFilter(tenant_id=tenant_id), category="random")
            )

        # High-frequency function words: the expected-divergence bucket source.
        for word in ("之", "而", "以", "也", "者", "于"):
            queries.append(
                SampleQuery(
                    text=word,
                    flt=SearchFilter(tenant_id=tenant_id),
                    category="function_word",
                )
            )
        # Single-character queries: a tokenizer boundary case.
        for _ in range(4):
            queries.append(
                SampleQuery(
                    text=_slice()[:1],
                    flt=SearchFilter(tenant_id=tenant_id),
                    category="single_char",
                )
            )
        # Guaranteed empty result: a token that cannot appear in the corpus.
        queries.append(
            SampleQuery(
                text="𓂀𓃭𓆣",
                flt=SearchFilter(tenant_id=tenant_id),
                category="empty_result",
            )
        )

        # Filtered queries: one narrow (single document) and one wide (source).
        if doc_ids:
            queries.append(
                SampleQuery(
                    text=_slice(),
                    flt=SearchFilter(tenant_id=tenant_id, document_ids=[rng.choice(doc_ids)]),
                    category="filtered_narrow",
                    filter_tier="narrow",
                )
            )
        if source_ids:
            queries.append(
                SampleQuery(
                    text=_slice(),
                    flt=SearchFilter(tenant_id=tenant_id, source_ids=[rng.choice(source_ids)]),
                    category="filtered_wide",
                    filter_tier="wide",
                )
            )

    return annotate_df(tenant_id, queries)


def annotate_df(tenant_id: str, queries: list[SampleQuery]) -> list[SampleQuery]:
    """Tag each query with the max document-frequency ratio of its terms.

    Counts df by streaming `chunk.text` through the shared tokenizer once -
    the same tokenizer every lexical backend runs, so the frequency reflects
    what is actually indexed. A query whose terms never occur gets ratio 0.
    """
    df: dict[str, int] = {}
    total = 0
    with session_scope() as session:
        for (text,) in session.execute(
            select(Chunk.text).where(Chunk.tenant_id == tenant_id)
        ):
            total += 1
            for term in set(tokenize(text)):
                df[term] = df.get(term, 0) + 1

    if total == 0:
        return queries

    annotated = []
    for q in queries:
        terms = tokenize(q.text)
        max_ratio = max((df.get(t, 0) / total for t in terms), default=0.0)
        annotated.append(
            SampleQuery(
                text=q.text,
                flt=q.flt,
                category=q.category,
                filter_tier=q.filter_tier,
                max_df_ratio=max_ratio,
            )
        )
    return annotated


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def _hard_cell(
    name: str,
    queries: list[SampleQuery],
    old: Callable[[SampleQuery], list[str]],
    new: Callable[[SampleQuery], list[str]],
    *,
    bucket_high_df: bool,
) -> CellResult:
    """Top-k SET equality. Order is deliberately ignored (tie-break differences
    between engines are not regressions). When *bucket_high_df*, queries at/above
    HIGH_DF_RATIO are counted as expected divergence and not failed."""
    result = CellResult(name=name, kind="hard")
    strata: dict[str, list[int]] = {}
    for q in queries:
        result.total += 1
        old_ids = set(old(q))
        new_ids = set(new(q))
        in_high_df = bucket_high_df and q.max_df_ratio >= HIGH_DF_RATIO

        key = "high_df" if in_high_df else "low_df"
        stat = strata.setdefault(key, [0, 0])
        stat[0] += 1

        if old_ids == new_ids:
            result.passed += 1
            stat[1] += 1
        elif in_high_df:
            result.expected_divergence += 1
    result.strata = {k: {"total": v[0], "passed": v[1]} for k, v in strata.items()}
    return result


def _recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 1.0 if not retrieved else 0.0
    got = set(retrieved[:k]) & relevant
    return len(got) / len(relevant)


def _soft_dense_cell(
    name: str,
    queries: list[SampleQuery],
    old_dense: DenseRetriever,
    new_dense: DenseRetriever,
    exact_dense: DenseRetriever,
    embed: Callable[[str], list[float]],
) -> CellResult:
    """Recall@10 of each approximate stack against exact brute force.

    Passes when new recall >= old recall per query; a lower new recall is a
    regression. Stratified by filter tier (none / narrow / wide) so a plan
    regression on filtered search cannot hide in the aggregate.
    """
    result = CellResult(name=name, kind="soft")
    old_recalls: list[float] = []
    new_recalls: list[float] = []
    strata: dict[str, list[float]] = {}
    for q in queries:
        vec = embed(q.text)
        truth = set(exact_dense(vec, q.flt))
        old_r = _recall_at_k(old_dense(vec, q.flt), truth, TOP_K)
        new_r = _recall_at_k(new_dense(vec, q.flt), truth, TOP_K)
        old_recalls.append(old_r)
        new_recalls.append(new_r)
        result.total += 1
        if new_r >= old_r:
            result.passed += 1
        else:
            result.regressions += 1
            logger.warning(
                "dense recall regression on %r: old=%.4f new=%.4f", q.text, old_r, new_r
            )
        bucket = strata.setdefault(q.filter_tier, [])
        bucket.append(new_r - old_r)

    result.recall_old = sum(old_recalls) / len(old_recalls) if old_recalls else None
    result.recall_new = sum(new_recalls) / len(new_recalls) if new_recalls else None
    result.strata = {
        tier: {
            "queries": len(deltas),
            "mean_delta": sum(deltas) / len(deltas) if deltas else 0.0,
            "regressions": sum(1 for d in deltas if d < 0),
        }
        for tier, deltas in strata.items()
    }
    return result


# ---------------------------------------------------------------------------
# Stack loading + orchestration
# ---------------------------------------------------------------------------


@dataclass
class Stack:
    """One side of the A/B comparison."""

    dense: DenseRetriever | None = None
    lexical: LexicalRetriever | None = None
    exact_dense: DenseRetriever | None = None
    close: Callable[[], None] = lambda: None


def _lexical_ids(store) -> LexicalRetriever:
    def _search(query: str, flt: SearchFilter) -> list[str]:
        return [h.id for h in store.search(query, limit=TOP_K, flt=flt)]

    return _search


def _dense_ids(store, *, exact: bool = False) -> DenseRetriever:
    def _search(vec: list[float], flt: SearchFilter) -> list[str]:
        if exact:
            return [
                h.id
                for h in store.search_dense(
                    vec, limit=TOP_K, flt=flt, _force_exact_scan=True
                )
            ]
        return [h.id for h in store.search_dense(vec, limit=TOP_K, flt=flt)]

    return _search


def load_stacks(profile: str) -> tuple[Stack, Stack]:
    """Construct old and new stores directly, both alive in one process.

    Constructing them explicitly (instead of flipping KB_*_BACKEND and
    resetting singletons) avoids the directory-lock dance: the old embedded
    Qdrant and Tantivy each take a lock, but those locks do not conflict with
    the SQLite/Postgres engine the new in-database stores share. The four
    stores coexist for the duration of the run.

    Calls `ensure_collection(dim)` on the new dense store before handing it
    back - load-bearing, not idempotent housekeeping. `PgVectorStore`/
    `SqliteVecStore.search_dense` both start with `if self._dim is None:
    return []` (a real store, freshly constructed by this function, has
    never had `ensure_collection` called on it - the *data* was written by
    an earlier `migrate-vectors` run in a different process, but this
    Python object's own `_dim` starts unset). Skipping this call does not
    raise anywhere - it makes every `new.dense`/`new.exact_dense` query
    silently return `[]`. For the server profile's *soft* cell that is
    doubly dangerous: both `new_dense` and `exact_dense` (the ground truth)
    come from the same under-initialised `pg` instance, so every query's
    "relevant" set and "retrieved" set are both empty - `_recall_at_k`
    defines an empty-vs-empty comparison as a pass - and the cell reports a
    perfect `Recall@10 1.0000` while never having executed a real query.
    Confirmed against the live dev database: this is exactly what produced
    `旧 0.0000 → 新 1.0000` on a first real run - Qdrant genuinely searched
    and came back with results (scored against an empty ground truth, so
    `_recall_at_k` counted it as a miss), while pgvector's "perfect" score
    was two empty sets agreeing with each other.
    """
    from ..embedding import get_dense_embedder
    from ..lexical.fts5_store import Fts5LexicalStore
    from ..lexical.pg_search_store import PgSearchLexicalStore
    from ..lexical.tantivy_store import TantivyLexicalStore
    from ..vector.pgvector_store import PgVectorStore
    from ..vector.qdrant_store import QdrantVectorStore
    from ..vector.sqlite_vec_store import SqliteVecStore

    old = Stack()
    new = Stack()
    stores = []
    dim = get_dense_embedder().dim

    if profile == "local":
        q = QdrantVectorStore()
        t = TantivyLexicalStore()
        s = SqliteVecStore()
        f = Fts5LexicalStore()
        s.ensure_collection(dim)
        stores = [q, t, s, f]
        old.dense = _dense_ids(q)
        old.lexical = _lexical_ids(t)
        new.dense = _dense_ids(s)
        new.lexical = _lexical_ids(f)
        # Local dense is exact brute force on both sides, so the hard cell
        # compares Qdrant vs sqlite-vec with no separate ground truth.
    elif profile == "server":
        q = QdrantVectorStore()
        t = TantivyLexicalStore()
        pg = PgVectorStore()
        ps = PgSearchLexicalStore()
        pg.ensure_collection(dim)
        stores = [q, t, pg, ps]
        old.dense = _dense_ids(q)
        old.lexical = _lexical_ids(t)
        new.dense = _dense_ids(pg)
        new.lexical = _lexical_ids(ps)
        # Ground truth for the soft cell: exact scan on pgvector.
        new.exact_dense = _dense_ids(pg, exact=True)
    else:
        raise ValueError(f"unknown profile {profile!r}; expected 'local' or 'server'")

    def _close() -> None:
        for st in stores:
            close = getattr(st, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    close()

    old.close = new.close = _close
    return old, new


def run(
    profile: str,
    tenant_id: str,
    *,
    query_count: int = 1000,
    seed: int = 42,
    queries: list[SampleQuery] | None = None,
    stacks: tuple[Stack, Stack] | None = None,
    embed: Callable[[str], list[float]] | None = None,
) -> Report:
    """Run the four-cell gate. Most inputs are injectable for testing; the CLI
    path supplies none and gets real stores/queries/embedder."""
    if queries is None:
        queries = generate_queries(tenant_id, count=query_count, seed=seed)

    owns_stacks = stacks is None
    if stacks is None:
        old_stack, new_stack = load_stacks(profile)
    else:
        old_stack, new_stack = stacks
    try:
        cells: list[CellResult] = []

        lexical = _hard_cell(
            f"{profile} lexical",
            queries,
            old=lambda q: old_stack.lexical(q.text, q.flt),
            new=lambda q: new_stack.lexical(q.text, q.flt),
            bucket_high_df=True,
        )
        cells.append(lexical)

        if profile == "local":
            # Embed each query once and feed the same vector to both stacks -
            # embedding twice would be wasteful and could in principle diverge.
            vec_cache: dict[str, list[float]] = {}

            def _vec(text: str) -> list[float]:
                v = vec_cache.get(text)
                if v is None:
                    v = _embed_one(embed, text)
                    vec_cache[text] = v
                return v

            dense = _hard_cell(
                "local dense",
                queries,
                old=lambda q: old_stack.dense(_vec(q.text), q.flt),
                new=lambda q: new_stack.dense(_vec(q.text), q.flt),
                bucket_high_df=False,
            )
        else:
            if embed is None:
                from ..embedding import get_dense_embedder

                embedder = get_dense_embedder()
                embed = embedder.embed_query
            dense = _soft_dense_cell(
                "server dense",
                queries,
                old_stack.dense,
                new_stack.dense,
                new_stack.exact_dense,
                embed,
            )
        cells.append(dense)
    finally:
        if owns_stacks:
            old_stack.close()

    return Report(profile=profile, cells=cells, query_count=len(queries))


def _embed_one(embed, text: str) -> list[float]:
    if embed is not None:
        return embed(text)
    from ..embedding import get_dense_embedder

    return get_dense_embedder().embed_query(text)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class Report:
    profile: str
    cells: list[CellResult]
    query_count: int

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.cells)

    def render(self) -> str:
        lines: list[str] = []
        for c in self.cells:
            if c.kind == "hard":
                low = c.strata.get("low_df", {"total": 0, "passed": 0})
                high = c.strata.get("high_df", {"total": 0, "passed": 0})
                lines.append(
                    f"硬断言   {c.name:<14} {low['passed']}/{low['total']} 通过  (df<50% 桶)"
                )
                if high["total"]:
                    lines.append(
                        f"                       {high['total'] - high['passed']} 条分歧"
                        f"       (df≥50% 桶，预期内)"
                    )
            else:
                arrow = "✓ 不低于" if c.ok else "✗ 回退"
                lines.append(
                    f"软断言   {c.name:<14} Recall@{TOP_K}  "
                    f"旧 {c.recall_old:.4f} → 新 {c.recall_new:.4f}   {arrow}"
                )
                for tier, stat in c.strata.items():
                    lines.append(
                        f"                       {tier:<6}: {stat['queries']} 查询, "
                        f"Δ均值 {stat['mean_delta']:+.4f}, 回退 {stat['regressions']}"
                    )
        lines.append("")
        lines.append("结果: " + ("PASS — 可以翻开关" if self.ok else "FAIL — 不得切换后端"))
        return "\n".join(lines)

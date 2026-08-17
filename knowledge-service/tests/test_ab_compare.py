"""Tests for the ADR-0008 A/B acceptance harness (ticket 10).

The acceptance criterion is that the framework itself is correct: a fake
backend with a known difference must be caught by the right cell, and the
high-df expected-divergence bucket must not fail. The four-cell `run()` path
against real stores is exercised end-to-end in ticket 11; here the cell logic
is the unit under test.
"""

from __future__ import annotations

import pytest

from kbsvc.retrieval.ab_compare import (
    HIGH_DF_RATIO,
    CellResult,
    Report,
    SampleQuery,
    _hard_cell,
    _recall_at_k,
    _soft_dense_cell,
    annotate_df,
    generate_queries,
)
from kbsvc.vector.base import SearchFilter


def _q(text: str, max_df: float = 0.1, tier: str = "none") -> SampleQuery:
    return SampleQuery(
        text=text,
        flt=SearchFilter(tenant_id="t"),
        category="random",
        filter_tier=tier,
        max_df_ratio=max_df,
    )


# --- hard cell -----------------------------------------------------------


def test_hard_cell_ignores_order_and_passes_on_equal_sets():
    queries = [_q(f"q{i}") for i in range(5)]

    # Old returns ids in one order, new in another - same set.
    def old(q):
        return ["a", "b", "c", "d", "e"]

    def new(q):
        return ["e", "d", "c", "b", "a"]

    result = _hard_cell("lex", queries, old, new, bucket_high_df=True)

    assert result.kind == "hard"
    assert result.total == 5
    assert result.passed == 5
    assert result.ok


def test_hard_cell_catches_a_real_difference():
    queries = [_q("same"), _q("different", max_df=0.1)]

    def old(q):
        return ["a", "b", "c"]

    # The second query gets a divergent set; the first matches.
    def new(q):
        return ["a", "b", "c"] if q.text == "same" else ["x", "y", "z"]

    result = _hard_cell("lex", queries, old, new, bucket_high_df=True)

    assert result.total == 2
    assert result.passed == 1
    assert not result.ok
    assert result.expected_divergence == 0


def test_hard_cell_buckets_high_df_divergence_as_expected():
    # A high-df query where the two engines differ: that is the measured,
    # expected IDF-clamp divergence, and must NOT fail the gate.
    high = _q("之", max_df=HIGH_DF_RATIO)
    low = _q("rare", max_df=0.01)

    def old(q):
        return ["a", "b"]

    def new(q):
        return ["c", "d"]  # both diverge

    result = _hard_cell("lex", [high, low], old, new, bucket_high_df=True)

    assert result.total == 2
    assert result.passed == 0
    assert result.expected_divergence == 1  # only the high-df query
    # The low-df divergence is a real failure; the high-df one is excused.
    assert not result.ok
    strata = result.strata["high_df"]
    assert strata["total"] == 1 and strata["passed"] == 0


def test_hard_cell_does_not_bucket_when_told_not_to():
    # The dense cells do not have a high-df exemption: a dense difference at
    # any df is a real failure.
    high = _q("之", max_df=HIGH_DF_RATIO)

    def old(q):
        return ["a"]

    def new(q):
        return ["b"]

    result = _hard_cell("dense", [high], old, new, bucket_high_df=False)

    assert result.expected_divergence == 0
    assert not result.ok


# --- soft cell -----------------------------------------------------------


def test_recall_at_k():
    assert _recall_at_k(["a", "b", "c"], {"a", "c"}, 3) == 1.0
    assert _recall_at_k(["a", "x", "y"], {"a", "c"}, 3) == 0.5
    # Empty ground truth is only "recalled" by an empty result.
    assert _recall_at_k([], set(), 10) == 1.0
    assert _recall_at_k(["a"], set(), 10) == 0.0


def test_soft_cell_passes_when_new_not_worse():
    queries = [_q("q", tier="none")]
    truth = {f"id{i}" for i in range(10)}

    # Both stacks return the truth set: recall 1.0 each.
    def exact(vec, flt):
        return list(truth)

    def old(vec, flt):
        return list(truth)

    def new(vec, flt):
        return list(truth)

    def embed(text):
        return [0.0]

    result = _soft_dense_cell(
        "server dense", queries, old, new, exact, embed=embed
    )

    assert result.kind == "soft"
    assert result.ok
    assert result.regressions == 0
    assert result.recall_old == 1.0
    assert result.recall_new == 1.0


def test_soft_cell_catches_a_recall_regression():
    queries = [_q("q", tier="narrow")]
    truth = {f"id{i}" for i in range(10)}

    def exact(vec, flt):
        return list(truth)

    def old(vec, flt):
        return list(truth)  # recall 1.0

    def new(vec, flt):
        return ["id0"]  # recall 0.1 -> regression

    def embed(text):
        return [0.0]

    result = _soft_dense_cell(
        "server dense", queries, old, new, exact, embed=embed
    )

    assert result.regressions == 1
    assert not result.ok
    assert result.recall_new < result.recall_old
    # The narrow-filter tier must record the regression so it can't hide in
    # the aggregate.
    assert result.strata["narrow"]["regressions"] == 1


def test_soft_cell_tolerates_new_being_better():
    queries = [_q("q")]
    truth = {"a", "b", "c", "d"}

    def exact(vec, flt):
        return list(truth)

    def old(vec, flt):
        return ["a"]  # 0.25

    def new(vec, flt):
        return ["a", "b"]  # 0.5 >= old, passes

    def embed(text):
        return [0.0]

    result = _soft_dense_cell(
        "server dense", queries, old, new, exact, embed=embed
    )

    assert result.ok
    assert result.passed == 1


# --- report --------------------------------------------------------------


def test_report_ok_reflects_all_cells():
    passing = CellResult(name="a", kind="hard", total=1, passed=1)
    failing = CellResult(name="b", kind="hard", total=1, passed=0)
    assert Report("local", [passing], 1).ok
    assert not Report("local", [passing, failing], 1).ok


def test_report_renders_pass_and_fail_lines():
    cells = [
        CellResult(
            name="local lexical",
            kind="hard",
            total=12,
            passed=10,
            expected_divergence=2,
            strata={
                "low_df": {"total": 10, "passed": 10},
                "high_df": {"total": 2, "passed": 0},
            },
        ),
        CellResult(
            name="local dense",
            kind="hard",
            total=12,
            passed=12,
            expected_divergence=0,
            strata={"low_df": {"total": 12, "passed": 12}},
        ),
    ]
    text = Report("local", cells, 12).render()
    assert "PASS" in text
    assert "df<50%" in text
    assert "预期内" in text


# --- query synthesis -----------------------------------------------------


# Unique ids so this corpus never collides with another test's rows in the
# shared session database, and can be torn down precisely.
_TENANT = "ab_harness_tenant"
_DOC = "ab_harness_doc"
_VER = "ab_harness_version"


def _cleanup(session) -> None:
    from kbsvc.db.models import Chunk, Document, DocumentVersion

    for chunk in session.query(Chunk).filter_by(tenant_id=_TENANT).all():
        session.delete(chunk)
    for ver in session.query(DocumentVersion).filter_by(tenant_id=_TENANT).all():
        session.delete(ver)
    for doc in session.query(Document).filter_by(tenant_id=_TENANT).all():
        session.delete(doc)
    session.commit()


def _seed_corpus(session, rows: list[tuple[str, str]]) -> None:
    """Insert tenant/document/chunk ORM rows and commit, so a second session
    (opened by generate_queries) can read them. Using ORM objects applies the
    server-side defaults (created_at, etc.) that raw INSERTs bypass. The rows
    are removed again at teardown so this test does not leak chunks into the
    shared database - other tests count chunks across tenants."""
    from kbsvc.db.models import Chunk, Document, DocumentVersion, Tenant

    _cleanup(session)
    ten = session.get(Tenant, _TENANT)
    if ten is None:
        ten = Tenant(id=_TENANT, name=_TENANT)
        session.add(ten)
    doc = Document(
        id=_DOC, tenant_id=_TENANT, source_id="ab_source", external_id="ab_ext",
        title="title", acl=["public"],
    )
    session.add(doc)
    session.add(
        DocumentVersion(
            id=_VER, document_id=_DOC, tenant_id=_TENANT, version=1,
            content_hash="abseedhash",
        )
    )
    for i, (cid, body) in enumerate(rows):
        session.add(
            Chunk(
                id=cid, tenant_id=_TENANT, document_id=_DOC, version_id=_VER, ordinal=i,
                kind="text", text=body, analyzed="",
            )
        )
    session.commit()


@pytest.fixture
def seeded_corpus(request, session):
    """Seed + guarantee cleanup even on assertion failure."""
    def _seed(rows):
        _seed_corpus(session, rows)
        request.addfinalizer(lambda: _cleanup(session))
    return _seed


def test_generate_queries_is_reproducible_and_annotated(session, seeded_corpus):
    # Plenty of text so 8-20 char slices are always available.
    body = "子曰學而時習之不亦說乎有朋自遠方來不亦樂乎人不知而不慍不亦君子乎" * 5
    seeded_corpus([("ab_c1", body)])

    first = generate_queries(_TENANT, count=50, seed=123)
    second = generate_queries(_TENANT, count=50, seed=123)

    assert len(first) == len(second)
    assert [q.text for q in first] == [q.text for q in second]
    assert all(0.0 <= q.max_df_ratio <= 1.0 for q in first)
    categories = {q.category for q in first}
    assert "function_word" in categories
    assert "empty_result" in categories
    assert "filtered_narrow" in categories
    empty = next(q for q in first if q.category == "empty_result")
    assert empty.max_df_ratio == 0.0


def test_annotate_df_computes_max_term_frequency(session, seeded_corpus):
    # 4 chunks; term "之" appears in every chunk -> ratio 1.0.
    seeded_corpus([(f"ab_c{i}", "之 甲 乙") for i in range(4)])

    [q] = annotate_df(_TENANT, [_q("之")])
    assert q.max_df_ratio == 1.0

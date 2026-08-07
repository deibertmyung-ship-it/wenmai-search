"""ProjectionBuilder against a real PostgreSQL server.

Needs the knowledge-base tables (documents, versions, chunks) alongside the
`plag_*` ones, so it builds both on the test database.
"""

from __future__ import annotations

import pytest

from kbsvc import ids
from kbsvc.config import get_settings
from kbsvc.db.models import Base, Chunk, Document, DocumentVersion, Source
from kbsvc.plagiarism import repository as repo
from kbsvc.plagiarism.models import PlagCorpusChunk, PlagCorpusProjection
from kbsvc.plagiarism.projection import ProjectionBuilder, run_corpus_job
from kbsvc.plagiarism.types import CorpusJobStatus

pytest.importorskip("pysbd", reason="projection needs the `plagiarism` extra")

TENANT = "test"
BODY = (
    "贼克者，取用之首法也。上克下为贼，下贼上为克。"
    "凡四课之中，有上克下者为贼，下贼上者为克。"
    "取用之道，先取贼克，次取比用。涉害者，比用不成则涉害。"
)


@pytest.fixture(scope="session")
def kb_schema(pg_schema):
    """The knowledge-base tables, on the same test database."""
    Base.metadata.create_all(pg_schema)
    yield pg_schema
    Base.metadata.drop_all(pg_schema)


@pytest.fixture
def kb_session(kb_schema, pg_session):
    """Clean the knowledge-base tables between cases too."""
    yield pg_session
    for model in (Chunk, DocumentVersion, Document, Source):
        pg_session.query(model).delete()
    pg_session.commit()


def seed_document(session, *, text=BODY, document_id=None, version_no=1):
    """A minimal indexed document: source, document, version, one chunk."""
    source = session.get(Source, "src-1") or Source(
        id="src-1", tenant_id=TENANT, name="pytest", kind="upload"
    )
    session.merge(source)

    document_id = document_id or ids.new_id()
    version_id = ids.new_id()
    document = Document(
        id=document_id,
        tenant_id=TENANT,
        source_id="src-1",
        external_id=f"ext-{document_id}",
        title="六壬大全",
        acl=["public"],
    )
    version = DocumentVersion(
        id=version_id,
        tenant_id=TENANT,
        document_id=document_id,
        version=version_no,
        # Unique per version: (document_id, content_hash) is constrained.
        content_hash=f"{version_no:064d}",
        mime="text/plain",
        size_bytes=len(text),
        object_key="k",
        status="indexed",
    )
    chunk = Chunk(
        id=ids.new_id(),
        tenant_id=TENANT,
        document_id=document_id,
        version_id=version_id,
        ordinal=0,
        text=text,
        char_start=0,
        char_end=len(text),
    )
    session.merge(document)
    session.add_all([version, chunk])
    document.current_version_id = version_id
    session.flush()
    return document_id, version_id


def enqueue(session, document_id, version_id):
    settings = get_settings()
    return repo.enqueue_corpus_job(
        session,
        tenant_id=TENANT,
        document_id=document_id,
        version_id=version_id,
        algorithm_config_hash=settings.plagiarism_algorithm_config_hash,
        max_attempts=3,
    )


# --- building -----------------------------------------------------------


def test_build_produces_an_activated_projection_with_fingerprinted_chunks(kb_session):
    document_id, version_id = seed_document(kb_session)
    job = enqueue(kb_session, document_id, version_id)

    result = ProjectionBuilder().build(kb_session, job.id)

    projection = kb_session.get(PlagCorpusProjection, result["projection_id"])
    assert projection.active_from is not None
    assert projection.active_until is None
    assert projection.chunk_count > 0

    chunks = (
        kb_session.query(PlagCorpusChunk)
        .filter_by(projection_id=projection.id)
        .order_by(PlagCorpusChunk.chunk_index)
        .all()
    )
    assert chunks
    assert all(chunk.fingerprints for chunk in chunks)


def test_chunk_offsets_slice_back_to_the_document_text(kb_session):
    """Offsets are what a finding is reported against; if they drift from the
    knowledge base's own text, every highlighted span is wrong."""
    document_id, version_id = seed_document(kb_session)
    job = enqueue(kb_session, document_id, version_id)
    result = ProjectionBuilder().build(kb_session, job.id)

    chunks = kb_session.query(PlagCorpusChunk).filter_by(
        projection_id=result["projection_id"]
    ).all()
    for chunk in chunks:
        assert BODY[chunk.char_start : chunk.char_end] == chunk.text


def test_language_is_detected_as_chinese(kb_session):
    document_id, version_id = seed_document(kb_session)
    job = enqueue(kb_session, document_id, version_id)
    result = ProjectionBuilder().build(kb_session, job.id)
    assert result["language"].startswith("zh")


def test_rebuild_retires_the_previous_projection_atomically(kb_session):
    """The corpus must never have a window where the document is absent."""
    document_id, version_id = seed_document(kb_session)
    first = ProjectionBuilder().build(kb_session, enqueue(kb_session, document_id, version_id).id)

    _, version_two = seed_document(kb_session, document_id=document_id, version_no=2)
    second = ProjectionBuilder().build(kb_session, enqueue(kb_session, document_id, version_two).id)

    old = kb_session.get(PlagCorpusProjection, first["projection_id"])
    new = kb_session.get(PlagCorpusProjection, second["projection_id"])
    assert old.active_until == new.active_from
    assert new.active_until is None


def test_a_document_with_no_text_still_yields_a_projection(kb_session):
    """An image-only source is legitimately empty. Refusing to publish would
    leave it permanently 'pending' and block every check."""
    document_id, version_id = seed_document(kb_session, text="")
    job = enqueue(kb_session, document_id, version_id)
    result = ProjectionBuilder().build(kb_session, job.id)
    projection = kb_session.get(PlagCorpusProjection, result["projection_id"])
    assert projection.active_from is not None
    assert projection.chunk_count == 0


# --- job settlement -----------------------------------------------------


def test_successful_run_completes_the_job(kb_session):
    document_id, version_id = seed_document(kb_session)
    job = enqueue(kb_session, document_id, version_id)
    run_corpus_job(kb_session, job.id)
    assert job.status == CorpusJobStatus.COMPLETED


def test_a_failed_build_leaves_the_previous_projection_live(kb_session):
    """A broken rebuild must not take the document out of the corpus."""
    document_id, version_id = seed_document(kb_session)
    good = ProjectionBuilder().build(kb_session, enqueue(kb_session, document_id, version_id).id)
    kb_session.commit()

    broken = repo.enqueue_corpus_job(
        kb_session,
        tenant_id=TENANT,
        document_id=document_id,
        version_id="missing-version",
        algorithm_config_hash=get_settings().plagiarism_algorithm_config_hash,
        max_attempts=1,
    )
    with pytest.raises(ValueError):
        run_corpus_job(kb_session, broken.id)

    still_live = kb_session.get(PlagCorpusProjection, good["projection_id"])
    assert still_live.active_until is None


# --- document frequency -------------------------------------------------


def test_df_counts_documents_not_chunk_repeats(kb_session):
    """A phrase repeated inside one book is one document's worth of evidence;
    counting chunks would let a repetitive source stop-list a term for all."""
    repeated = BODY * 4
    document_id, version_id = seed_document(kb_session, text=repeated)
    ProjectionBuilder().build(kb_session, enqueue(kb_session, document_id, version_id).id)

    count = repo.rebuild_fingerprint_df(
        kb_session,
        tenant_id=TENANT,
        algorithm_config_hash=get_settings().plagiarism_algorithm_config_hash,
    )
    assert count > 0
    corpus_size = repo.live_projection_count(
        kb_session,
        tenant_id=TENANT,
        algorithm_config_hash=get_settings().plagiarism_algorithm_config_hash,
    )
    assert corpus_size == 1
    frequent = repo.high_frequency_fingerprints(
        kb_session,
        tenant_id=TENANT,
        algorithm_config_hash=get_settings().plagiarism_algorithm_config_hash,
        ratio_threshold=0.5,
        corpus_size=corpus_size,
    )
    # One document in the corpus: every fingerprint has DF 1, which is 100%.
    assert frequent

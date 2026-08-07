"""Plagiarism detection.

The module's external seam. HTTP routes, the CLI and the ingest worker depend
only on what is exported here - never on the fingerprinting, candidate-retrieval
or alignment internals, and never on the `plag_*` database models.

`PlagiarismService` lands in a later phase; until then this exports the domain
types the seam is expressed in.

Algorithm implementations under this package are ported from noplag-engine
(Apache-2.0) - see `THIRD_PARTY_NOTICES.md` and ADR-0001.
"""

from __future__ import annotations

from .types import (
    ACTIVE_CHECK_STATUSES,
    TERMINAL_CHECK_STATUSES,
    CheckEvent,
    CheckReport,
    CheckStage,
    CheckStatus,
    CheckSummary,
    CorpusJobStatus,
    CorpusStatus,
    CoverageReason,
    CreateDocumentCheck,
    CreateTextCheck,
    MatchedPassage,
    MatchedSource,
)

__all__ = [
    "ACTIVE_CHECK_STATUSES",
    "TERMINAL_CHECK_STATUSES",
    "CheckEvent",
    "CheckReport",
    "CheckStage",
    "CheckStatus",
    "CheckSummary",
    "CorpusJobStatus",
    "CorpusStatus",
    "CoverageReason",
    "CreateDocumentCheck",
    "CreateTextCheck",
    "MatchedPassage",
    "MatchedSource",
]

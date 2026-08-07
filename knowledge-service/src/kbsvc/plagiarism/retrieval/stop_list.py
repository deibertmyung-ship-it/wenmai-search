"""Stop fingerprints - the non-discriminative tail of the corpus.

A fingerprint present in a large share of the corpus (boilerplate, stock
phrasing, in this corpus the formulaic openings shared across 术数 texts) makes
the `&&` probe touch a wide slice of the index for no discriminative gain. Both
recall and latency improve by dropping it from the probe.

Ported in concept from noplag-engine `src/noplag_engine/retrieval/stop_list.py`
at commit 005da60faad21bf52702997d73583b78d8905d22, Apache-2.0 (see
`third_party/noplag-engine/LICENSE-APACHE-2.0.txt`).

Modified for kbsvc, substantially:

* **Query-side only.** Upstream strips stop fingerprints at *both* ingest and
  query time, and its docstring warns at length that the two layers must agree
  or the index and the probe fall out of sync. Here the projection stores every
  fingerprint and filtering happens only when probing - so the hazard cannot
  arise, and re-tuning the threshold does not require re-fingerprinting the
  corpus. The content-hash cross-layer assertion upstream needs is therefore
  dropped along with the second layer.
* **Settings, not environment variables.** Algorithm code in kbsvc does not read
  `os.environ` (ADR-0001); the caller passes the thresholds in.
* **No process-level cache.** Upstream caches for the life of the process so its
  hash stays stable, at the cost of needing a restart after a DF rebuild. With
  one layer there is nothing to keep in sync, so the set is read per check and a
  `rebuild-df` goes live immediately.
* **Ratio first, count second.** Upstream selects a fixed top-N. A ratio is
  meaningful across corpus sizes; the count is retained only as a safety cap so
  a pathological DF distribution cannot stop-list most of the probe.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from sqlalchemy.orm import Session

from .. import repository as repo

logger = logging.getLogger(__name__)


def stop_fingerprints(
    session: Session,
    *,
    tenant_id: str,
    algorithm_config_hash: str,
    ratio_threshold: float,
    corpus_size: int,
) -> set[int]:
    """Fingerprints appearing in at least `ratio_threshold` of the corpus.

    Empty when the corpus is too small for the ratio to mean anything - with
    one or two documents every fingerprint trivially clears any threshold, and
    stopping them all would leave nothing to probe with.
    """
    # Below this, "appears in most documents" is not evidence of boilerplate.
    if corpus_size < 3:
        return set()
    return repo.high_frequency_fingerprints(
        session,
        tenant_id=tenant_id,
        algorithm_config_hash=algorithm_config_hash,
        ratio_threshold=ratio_threshold,
        corpus_size=corpus_size,
    )


_WORD_CHAR = re.compile(r"[\w㐀-䶿一-鿿豈-﫿]")


def is_discriminative(chunk_text: str, *, min_word_ratio: float) -> bool:
    """Whether a chunk carries enough real content to be worth probing with.

    A chunk that is mostly punctuation or box-drawing - a rule line, a table
    border, a run of dashes separating sections - matches every other such run
    in the corpus *exactly*, at score 1.0. Those are real verbatim matches and
    completely meaningless ones.

    Found by acceptance testing against the real corpus: a submission of 300
    dashes reported 100% reuse across four books. Real prose, classical or
    modern, produced zero false positives; only formatting did.

    The DF stop list cannot catch this. Separator runs appear in a handful of
    books, so their document frequency is low - they look rare, which is exactly
    what the stop list is built to keep.

    Upstream carried this guard inside `retrieval/l1_winnowing.py`, whose
    candidate-selection half was not ported (the GIN query replaces it). This is
    the part that still had to come across.
    """
    if not chunk_text:
        return False
    word_chars = sum(1 for _ in _WORD_CHAR.finditer(chunk_text))
    return word_chars / len(chunk_text) >= min_word_ratio


def apply(probe: Sequence[int], stopped: set[int]) -> list[int]:
    """Drop stopped fingerprints from a probe.

    Falls back to the unfiltered probe when filtering would empty it: a chunk
    made entirely of common phrasing still deserves its shot at retrieval, and
    an empty probe silently returns no candidates - indistinguishable from
    "nothing matched".
    """
    if not stopped:
        return list(probe)
    filtered = [fingerprint for fingerprint in probe if fingerprint not in stopped]
    if not filtered:
        logger.debug("probe was entirely stop fingerprints; using it unfiltered")
        return list(probe)
    return filtered

"""Lightweight sliding-window rate limiter (no external dependencies).

Thread-safe via a single lock.  Sufficient for a self-hosted system where the
worker count and client base are small; for larger deployments consider
``slowapi`` (FastAPI) or ``Flask-Limiter`` backed by Redis.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Per-key sliding-window counter.

    ``allow(key)`` returns True when the caller is under the limit and records
    the hit; False when the limit has been reached.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                return False
            hits.append(now)
            # Opportunistic cleanup prevents unbounded growth from one-off IPs.
            if len(self._hits) > 10_000:
                self._evict_stale(cutoff)
            return True

    def _evict_stale(self, cutoff: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del self._hits[k]

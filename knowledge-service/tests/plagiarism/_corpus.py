"""Shared constants for the plagiarism suite.

A plain module rather than fixtures: these are literals used in assertions and
in string arithmetic (`BODY * 4`), where fixture indirection buys nothing. Lives
next to `conftest.py`, which pytest puts on `sys.path` during collection.
"""

from __future__ import annotations

TENANT = "test"

# Classical Chinese with enough sentence structure for pysbd to segment. Shared
# so the projection and detection suites exercise the same text - a match found
# by one and missed by the other should be a real signal, not a fixture
# difference.
BODY = (
    "贼克者，取用之首法也。上克下为贼，下贼上为克。"
    "凡四课之中，有上克下者为贼，下贼上者为克。"
    "取用之道，先取贼克，次取比用。涉害者，比用不成则涉害。"
)

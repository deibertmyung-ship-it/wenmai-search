"""Frontend-facing errors.

The backend's error envelope is translated here into something a reader can act
on. Internal addresses and stack traces never reach the template.
"""

from __future__ import annotations


class BackendError(Exception):
    """kbsvc answered, but with an error."""

    def __init__(self, status: int, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}


class BackendUnavailable(Exception):
    """kbsvc could not be reached at all - network, DNS, timeout."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

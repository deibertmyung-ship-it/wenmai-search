"""Domain errors mapped to a single API error envelope."""

from __future__ import annotations


class KbError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_envelope(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "detail": self.detail}}


class NotFoundError(KbError):
    code = "not_found"
    http_status = 404


class ValidationError(KbError):
    code = "validation_error"
    http_status = 400


class AuthError(KbError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(KbError):
    code = "forbidden"
    http_status = 403


class PayloadTooLargeError(KbError):
    code = "payload_too_large"
    http_status = 413


class ParserError(KbError):
    code = "parser_error"
    http_status = 422


class DependencyMissingError(KbError):
    code = "dependency_missing"
    http_status = 503

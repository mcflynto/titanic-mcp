"""Stable error codes (spec section 8). Agents can read these codes and correct their next call."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    QUERY_REJECTED = "QUERY_REJECTED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    INTERNAL = "INTERNAL"


# Log level for each code (spec section 8, error table).
LOG_LEVEL: dict[ErrorCode, str] = {
    ErrorCode.UNAUTHENTICATED: "warning",
    ErrorCode.PERMISSION_DENIED: "warning",
    ErrorCode.RATE_LIMITED: "warning",
    ErrorCode.INVALID_ARGUMENT: "info",
    ErrorCode.NOT_FOUND: "info",
    ErrorCode.CONFLICT: "info",
    ErrorCode.QUERY_REJECTED: "warning",
    ErrorCode.QUERY_TIMEOUT: "warning",
    ErrorCode.RESULT_TOO_LARGE: "info",
    ErrorCode.DATABASE_UNAVAILABLE: "error",
    ErrorCode.INTERNAL: "error",
}


class AppError(Exception):
    """An error that the server sends to the agent as a tool error with structured content."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self, request_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code.value, "message": self.message, "request_id": request_id}
        if self.hint:
            body["hint"] = self.hint
        if self.details:
            body["details"] = self.details
        return body


def denied(message: str, hint: str | None = None) -> AppError:
    return AppError(ErrorCode.PERMISSION_DENIED, message, hint=hint)


def invalid(message: str, hint: str | None = None) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def rejected(message: str, hint: str | None = None) -> AppError:
    return AppError(ErrorCode.QUERY_REJECTED, message, hint=hint)

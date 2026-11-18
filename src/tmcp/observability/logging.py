"""Structured JSON logs to stdout (LOG-01 to LOG-05)."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace

from tmcp.observability.context import request_id_var

REDACTED = "[REDACTED]"
_SECRET_KEYS = {"authorization", "cookie", "token", "access_token", "api_key", "password", "secret", "client_secret"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if str(k).lower() in _SECRET_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


def redact_secrets(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """LOG-05: remove secrets from all log events."""
    return _redact(dict(event_dict))  # type: ignore[no-any-return,unused-ignore]


def add_request_and_trace_ids(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    request_id = request_id_var.get()
    if request_id and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = format(span_context.trace_id, "032x")
        event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict


def configure_logging(level: str, *, json_output: bool, service_version: str) -> None:
    def add_service(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        event_dict.setdefault("service_version", service_version)
        return event_dict

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_request_and_trace_ids,
        add_service,
        redact_secrets,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Send the logs of uvicorn, the MCP SDK and other libraries through the same JSON pipeline.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers[:] = []
        lib_logger.propagate = True
    # The HTTP middleware writes its own access log with the request ID.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)  # type: ignore[no-any-return,unused-ignore]

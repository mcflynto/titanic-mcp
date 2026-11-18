"""Audit events (AUD-01 to AUD-04). One event for each tool call and resource read, also for denied calls."""

from typing import Any

from tmcp.observability.context import client_ip_var, current_request_id
from tmcp.observability.logging import get_logger

_audit = get_logger("audit")
_MAX_VALUE_LENGTH = 2000


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_VALUE_LENGTH:
        return value[:_MAX_VALUE_LENGTH] + "...[truncated]"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value


def audit_event(
    *,
    agent_id: str | None,
    roles: list[str],
    tool: str,
    arguments: dict[str, Any],
    decision: str,
    deny_reason: str | None = None,
    error_code: str | None = None,
    row_count: int | None = None,
    truncated: bool | None = None,
    duration_ms: float,
) -> None:
    # AUD-04: the event never contains result rows.
    _audit.info(
        "audit",
        request_id=current_request_id(),
        agent_id=agent_id,
        roles=roles,
        tool=tool,
        arguments=_truncate(arguments),
        decision=decision,
        deny_reason=deny_reason,
        error_code=error_code,
        row_count=row_count,
        truncated=truncated,
        duration_ms=round(duration_ms, 2),
        client_ip=client_ip_var.get(),
    )

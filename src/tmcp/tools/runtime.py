"""Governance for each tool call: identity, permission, rate limit, trace span, metrics, audit, errors.

Each tool function gets the effective ``Permissions`` as its first argument. The wrapper removes this argument
from the public signature, so that the MCP SDK makes the input schema from the other arguments only.
"""

import functools
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel

from tmcp.config import Settings
from tmcp.db.base import ReadDatabase
from tmcp.db.catalog import Dataset
from tmcp.db.reference import ReferenceDatabase
from tmcp.db.working import WorkingDatabase
from tmcp.errors import LOG_LEVEL, AppError, ErrorCode
from tmcp.observability import metrics
from tmcp.observability.audit import audit_event
from tmcp.observability.context import current_request_id
from tmcp.observability.logging import get_logger
from tmcp.observability.tracing import tracer
from tmcp.policy.engine import Permissions
from tmcp.policy.store import PolicyStore
from tmcp.ratelimit import RateLimiter

log = get_logger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass
class AppState:
    settings: Settings
    version: str
    policy: PolicyStore
    reference: ReferenceDatabase
    working: WorkingDatabase | None
    limiter: RateLimiter

    def database(self, dataset: Dataset) -> ReadDatabase:
        if dataset == "reference":
            return self.reference
        if self.working is None:
            raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "The working dataset is not configured on this server.")
        return self.working

    def require_working(self) -> WorkingDatabase:
        if self.working is None:
            raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "The working dataset is not configured on this server.")
        return self.working


_state: AppState | None = None


def set_state(state: AppState | None) -> None:
    global _state
    _state = state


def state() -> AppState:
    if _state is None:
        raise AppError(ErrorCode.DATABASE_UNAVAILABLE, "The server is not ready.")
    return _state


def current_permissions() -> Permissions:
    token = get_access_token()
    if token is None:
        raise AppError(ErrorCode.UNAUTHENTICATED, "No authenticated agent.")
    roles = (token.claims or {}).get("roles") or []
    return state().policy.permissions(token.client_id, roles)


def _error_result(error: AppError) -> CallToolResult:
    body = {"error": error.to_dict(current_request_id())}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))], structured_content=body, is_error=True
    )


def _audit_arguments(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v) for k, v in kwargs.items()}


def governed_tool[ModelT: BaseModel](
    server: MCPServer,
    *,
    name: str,
    title: str,
    output: type[ModelT],
    read_only: bool = True,
    destructive: bool = False,
) -> Callable[[Callable[..., Awaitable[ModelT]]], Callable[..., Awaitable[CallToolResult]]]:
    def decorator(fn: Callable[..., Awaitable[ModelT]]) -> Callable[..., Awaitable[CallToolResult]]:
        signature = inspect.signature(fn)
        public_params = list(signature.parameters.values())[1:]  # remove 'perms'

        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> CallToolResult:
            start = time.perf_counter()
            perms: Permissions | None = None
            agent = "unknown"
            status, decision, deny_reason, error_code = "ok", "allow", None, None
            row_count: int | None = None
            truncated: bool | None = None
            with tracer.start_as_current_span(f"tool {name}") as span:
                try:
                    perms = current_permissions()
                    agent = metrics.agent_label(perms.agent_id, state().policy.known_agents)
                    span.set_attribute("tmcp.agent_id", perms.agent_id)
                    if not perms.can(name):
                        raise AppError(
                            ErrorCode.PERMISSION_DENIED,
                            f"Your roles {list(perms.roles)} do not permit the tool '{name}'.",
                            hint="Read the resource titanic://me to see your permissions.",
                        )
                    async with state().limiter.limit(perms.agent_id, perms.limits):
                        result = await fn(perms, **kwargs)
                    row_count = getattr(result, "row_count", None)
                    truncated = getattr(result, "truncated", None)
                    if row_count is not None:
                        metrics.QUERY_ROWS.labels(name).observe(row_count)
                    payload = result.model_dump(mode="json")
                    return CallToolResult(
                        content=[TextContent(type="text", text=json.dumps(payload, default=str))],
                        structured_content=payload,
                    )
                except AppError as exc:
                    status, error_code = exc.code.value, exc.code.value
                    if exc.code in (ErrorCode.PERMISSION_DENIED, ErrorCode.RATE_LIMITED, ErrorCode.UNAUTHENTICATED):
                        decision, deny_reason = "deny", exc.message
                    if exc.code == ErrorCode.PERMISSION_DENIED:
                        metrics.PERMISSION_DENIED.labels(agent, name).inc()
                    if exc.code == ErrorCode.RATE_LIMITED:
                        metrics.RATE_LIMITED.labels(agent).inc()
                    span.set_attribute("tmcp.error_code", exc.code.value)
                    getattr(log, LOG_LEVEL[exc.code])("tool_error", tool=name, code=exc.code.value, error=exc.message)
                    return _error_result(exc)
                except Exception as exc:
                    # LOG-03: the stack trace goes to the log, never to the agent.
                    status = error_code = ErrorCode.INTERNAL.value
                    span.record_exception(exc)
                    log.exception("tool_internal_error", tool=name)
                    return _error_result(AppError(ErrorCode.INTERNAL, "An internal error occurred."))
                finally:
                    duration = time.perf_counter() - start
                    metrics.REQUESTS.labels(name, status, agent).inc()
                    metrics.REQUEST_DURATION.labels(name).observe(duration)
                    audit_event(
                        agent_id=perms.agent_id if perms else None,
                        roles=list(perms.roles) if perms else [],
                        tool=name,
                        arguments=_audit_arguments(kwargs),
                        decision=decision,
                        deny_reason=deny_reason,
                        error_code=error_code,
                        row_count=row_count,
                        truncated=truncated,
                        duration_ms=duration * 1000,
                    )

        wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=public_params, return_annotation=Annotated[CallToolResult, output]
        )
        wrapper.__annotations__ = {p.name: p.annotation for p in public_params} | {
            "return": Annotated[CallToolResult, output]
        }
        server.add_tool(
            wrapper,
            name=name,
            title=title,
            description=inspect.cleandoc(fn.__doc__ or ""),
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=read_only,
                destructive_hint=destructive,
                idempotent_hint=read_only,
                open_world_hint=False,
            ),
        )
        return wrapper

    return decorator

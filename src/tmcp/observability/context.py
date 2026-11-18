"""Per-request context values. The HTTP middleware sets them; logs, audit events and errors read them."""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)


def current_request_id() -> str | None:
    return request_id_var.get()

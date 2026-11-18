"""Prometheus metrics (spec section 9). The server publishes them on a separate port."""

from prometheus_client import Counter, Gauge, Histogram

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

REQUESTS = Counter("tmcp_requests_total", "Number of MCP calls", ["tool", "status", "agent_id"])
REQUEST_DURATION = Histogram(
    "tmcp_request_duration_seconds", "Duration of MCP calls", ["tool"], buckets=_LATENCY_BUCKETS
)
QUERY_DURATION = Histogram(
    "tmcp_query_duration_seconds", "Duration of database queries", ["dataset"], buckets=_LATENCY_BUCKETS
)
QUERY_ROWS = Histogram(
    "tmcp_query_rows", "Rows for each result", ["tool"], buckets=(0, 1, 5, 10, 50, 100, 500, 1000, 5000)
)
AUTH_FAILURES = Counter("tmcp_auth_failures_total", "Failed authentications", ["reason"])
PERMISSION_DENIED = Counter("tmcp_permission_denied_total", "Denied calls", ["agent_id", "tool"])
RATE_LIMITED = Counter("tmcp_rate_limited_total", "Calls stopped by the rate limiter", ["agent_id"])
QUERY_REJECTED = Counter("tmcp_query_rejected_total", "SQL statements rejected by the validation", ["reason"])
INFLIGHT = Gauge("tmcp_inflight_requests", "HTTP requests in progress")
HTTP_REQUESTS = Counter("tmcp_http_requests_total", "HTTP requests", ["path", "status"])
POLICY_RELOAD_ERRORS = Counter("tmcp_policy_reload_errors_total", "Policy files that were not valid")
POLICY_RELOADS = Counter("tmcp_policy_reloads_total", "Policy files loaded")
CHANGE_REQUESTS = Counter("tmcp_change_requests_total", "Change requests by status transition", ["status"])
BUILD_INFO = Gauge(
    "tmcp_build_info", "Build and configuration data", ["version", "commit", "data_sha256", "policy_version"]
)


def agent_label(agent_id: str | None, known_agents: frozenset[str]) -> str:
    """Keep the label cardinality small: only agents from the policy file get their own value."""
    if agent_id and agent_id in known_agents:
        return agent_id
    return "unknown"


QUERY_REJECT_REASONS = (
    "column_permission", "cte_name", "forbidden_clause", "function", "length", "limit", "multiple_statements",
    "not_select", "parameter", "qualified_name", "syntax", "system_table", "table_function", "table_permission",
    "unknown_table",
)  # fmt: skip
AUTH_FAILURE_REASONS = (
    "expired", "invalid", "missing_agent_claim", "unknown_key", "wrong_issuer_or_audience", "unknown_api_key",
)  # fmt: skip
CHANGE_REQUEST_STATUSES = ("pending", "applied", "rejected", "conflict", "expired")


def initialize_series(agents: frozenset[str], tools: frozenset[str]) -> None:
    """Create all known label combinations with the value 0.

    Without this, a series appears for the first time with the value 1, and increase()/rate() in the alert
    rules see no change. Then the first SQL rejection or authentication failure of a pod never fires an alert.
    """
    for reason in QUERY_REJECT_REASONS:
        QUERY_REJECTED.labels(reason)
    for reason in AUTH_FAILURE_REASONS:
        AUTH_FAILURES.labels(reason)
    for status in CHANGE_REQUEST_STATUSES:
        CHANGE_REQUESTS.labels(status)
    for agent in (*agents, "unknown"):
        RATE_LIMITED.labels(agent)
        for tool in tools:
            PERMISSION_DENIED.labels(agent, tool)

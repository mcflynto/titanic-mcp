# Runbook: TmcpHighLatency

**Meaning:** The p95 latency of MCP calls is more than 1 s for 10 minutes.

**Procedure:**

1. Look at the panel 'Database query duration p95'. Find the dataset with the slow queries.
2. Look at the traces in Jaeger or your trace backend for the slow tool.
3. Examine CPU throttling and the HPA: `kubectl -n titanic-mcp get hpa`.
4. If one agent sends expensive SQL, reduce its `max_rows` or `query_timeout_s` in the policy.

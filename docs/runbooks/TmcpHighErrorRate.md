# Runbook: TmcpHighErrorRate

**Meaning:** More than 2 % of MCP calls fail with INTERNAL or DATABASE_UNAVAILABLE for 5 minutes.

**Procedure:**

1. Open the Grafana dashboard 'Titanic MCP Server'. Find the tool and the status with the most errors.
2. Read the error logs: `kubectl -n titanic-mcp logs -l app.kubernetes.io/name=titanic-mcp --prefix | grep '"level": "error"'`.
3. If the status is DATABASE_UNAVAILABLE: examine PostgreSQL (`kubectl -n titanic-mcp get pods -l app=postgres`) and `/healthz/ready`.
4. If the status is INTERNAL: find the stack trace with the request ID. If a new release caused it, do a rollback: `helm -n titanic-mcp rollback titanic-mcp`.

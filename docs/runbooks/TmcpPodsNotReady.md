# Runbook: TmcpPodsNotReady

**Meaning:** Fewer than 2 pods of the MCP server are ready for 5 minutes.

**Procedure:**

1. `kubectl -n titanic-mcp get pods` and `kubectl -n titanic-mcp describe pod <pod>`.
2. Call `/healthz/ready` on a pod. The response shows which check fails (reference_db, working_db, auth).
3. `auth` fails: the IdP JWKS is not available. `working_db` fails: PostgreSQL is not available.

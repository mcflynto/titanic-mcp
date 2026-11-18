# Runbook: TmcpSqlRejected

**Meaning:** The SQL guard rejected a dangerous statement (for example DROP, ATTACH, PRAGMA or a system table).

**Procedure:**

1. Find the audit event with `"error_code": "QUERY_REJECTED"`. It contains the agent ID and the SQL text.
2. One event can be a model error. Many events from one agent can be an attack through prompt injection.
3. If necessary, remove the role `sql-analyst` from the agent in the policy. The change is active in 60 seconds or less.

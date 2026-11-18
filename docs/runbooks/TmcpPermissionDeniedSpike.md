# Runbook: TmcpPermissionDeniedSpike

**Meaning:** One agent gets more than 10 denied calls per minute for 10 minutes. Usually the agent configuration is wrong.

**Procedure:**

1. Find the agent in the label `agent_id`.
2. Read its audit events: `... | grep '"audit"' | grep '"decision": "deny"'`.
3. Tell the agent team. If the agent needs more access, change the policy with a pull request.

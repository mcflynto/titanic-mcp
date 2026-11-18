# Onboarding guide for agent teams

This guide tells you how to connect your AI agent to the Titanic MCP Server.

## 1. Get an identity

1. Ask the MLOps team for an OAuth client. Give the agent name and the data that the agent needs.
2. The MLOps team adds your agent to `policy/policy.yaml` with a pull request. Your agent ID is the client ID.
3. You get a client ID and a client secret. Keep the secret in your secret store. Do not put it in Git.

## 2. Get a token

Use the OAuth 2.1 client credentials flow:

```bash
curl -s -X POST "$IDP/realms/titanic/protocol/openid-connect/token" \
  -d grant_type=client_credentials -d client_id="$CLIENT_ID" -d client_secret="$CLIENT_SECRET"
```

A token is valid for 5 minutes. Get a new token before it expires.

## 3. Connect

- Transport: Streamable HTTP, URL `https://<host>/mcp`.
- Header: `Authorization: Bearer <token>`.
- The server is stateless. Each request can go to a different pod.

Python example:

```python
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
async with Client(streamable_http_client("https://<host>/mcp", http_client=http)) as client:
    result = await client.call_tool("aggregate_passengers", {"group_by": ["class"]})
```

## 4. Find your permissions

Read the resource `titanic://me`. It shows your roles, tools, tables, columns, row filters and limits.
`tools/list` shows only the tools that you can call.

## 5. Read the errors

Each tool error has this format. Use `code` in your agent logic and `hint` to correct the next call.
Give `request_id` to the MLOps team when you report a problem.

```json
{"error": {"code": "PERMISSION_DENIED", "message": "...", "hint": "...", "request_id": "01J..."}}
```

| Code | What to do |
| --- | --- |
| `PERMISSION_DENIED` | Do not try again. Ask for a different role if necessary. |
| `RATE_LIMITED` | Wait `details.retry_after_s` seconds, then try again. |
| `INVALID_ARGUMENT`, `QUERY_REJECTED` | Correct the arguments. Read the hint. |
| `QUERY_TIMEOUT`, `RESULT_TOO_LARGE` | Add filters or aggregate the data. |
| `NOT_FOUND`, `CONFLICT` | Read the current state, then make a new request. |
| `DATABASE_UNAVAILABLE`, `INTERNAL` | Try again later with backoff. Report the request ID. |

## 6. Change data (working dataset only)

1. Call `propose_passenger_update` (or `_insert`, `_delete`) with a reason.
2. The result is a change request with the status `pending`.
3. A different agent with the role `approver` calls `review_change_request`.
4. After the approval, the change is in the dataset `working`. The dataset `reference` never changes.
5. If the row changed after your proposal, the status is `conflict`. Make a new proposal.
6. A pending change request expires after 7 days.

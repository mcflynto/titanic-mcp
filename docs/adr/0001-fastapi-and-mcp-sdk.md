# FastAPI with the official MCP Python SDK

Status: accepted (2026-11-18)

## Context

We need a proven HTTP framework and full MCP compatibility.

## Decision

We use FastAPI for health, version and middleware, and we mount the MCP SDK (`MCPServer`, Streamable HTTP) at `/mcp`. The SDK gives the protocol, the input and output schemas and the OAuth resource server support.

## Consequences

The SDK is at version 2.x and changes quickly. Renovate/Dependabot pull requests must examine the changelog (OP-08).

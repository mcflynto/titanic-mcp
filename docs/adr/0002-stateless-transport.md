# Stateless Streamable HTTP

Status: accepted (2026-11-18)

## Context

Kubernetes starts and stops pods at any time. Sticky sessions make scaling and rolling updates difficult.

## Decision

The server uses the stateless mode with JSON responses. Each request carries its token. Any pod can serve any request.

## Consequences

Server-to-client requests that need a session (for example sampling) are not available. The tools do not need them.

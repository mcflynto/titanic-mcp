# Titanic MCP Server

The Titanic MCP Server lets AI agents of other teams read and change the Titanic database through the
Model Context Protocol (MCP). The server controls which agent can see which data, records each access, and
operates in Kubernetes. The full specification is in [docs/specification.md](docs/specification.md): requirements with
IDs, the decisions OP-01 to OP-09, and the deviations of the implementation (section 16).

## Overview

| Item | Value |
| --- | --- |
| Language and framework | Python 3.12, FastAPI, official MCP Python SDK 2.x (Streamable HTTP, stateless) |
| Data | `reference`: the original `titanic.db` (read-only SQLite in the image). `working`: a writable PostgreSQL copy. |
| Authentication | OAuth 2.1 bearer tokens (JWT, Keycloak in the demo). API keys for local tests only. |
| Permissions | Policy file with roles: tool, table, column and row rules, k-anonymity, limits. |
| Writes | Change requests. A second agent with the role `approver` must approve them (four-eyes rule). |
| Observability | JSON logs, audit log, Prometheus metrics, OpenTelemetry traces, Grafana dashboard, alert rules. |
| Deployment | Distroless container image, Helm chart, kind demo cluster. |

## Quick start

Prerequisites: Docker, `uv`, `kind`, `kubectl`, `helm`.

```bash
make install        # Python packages
make check          # lint, types, tests (PostgreSQL tests need Docker), helm lint
make demo           # kind cluster + Keycloak + PostgreSQL + Jaeger + Prometheus/Grafana + MCP server
make demo-run       # presentation scenario (5 steps)
make demo-traffic   # traffic for the Grafana dashboard
make demo-rollout   # rolling update under load: expected result is 0 failed calls
make demo-down      # delete the cluster
```

Local run without Kubernetes: `make run` (API keys `dev-key-<agent-id>`, no working dataset), or
`make compose-up` (with PostgreSQL).

## Demo environment

| Component | URL | Credentials (placeholders) |
| --- | --- | --- |
| MCP server | http://localhost:18080/mcp | OAuth token from Keycloak |
| Keycloak | http://localhost:18081 | `admin` / `demo-admin-password-change-me` |
| Grafana | http://localhost:13000 | anonymous view; `admin` / `demo-grafana-password-change-me` |
| Prometheus | http://localhost:19090 | none |
| Jaeger | http://localhost:16686 | none |

Agents (Keycloak client ID = agent ID; secret = `<agent-id>-secret-change-me`):

| Agent | Roles | Can do |
| --- | --- | --- |
| `reporting-agent` | statistician | Aggregates only, groups of 5 or more |
| `churn-agent` | analyst | Rows of `passengers`, without `fare` |
| `steerage-agent` | analyst-third-class | Like analyst, only third-class rows |
| `research-agent` | sql-analyst | All reference tables, free read-only SQL |
| `curation-agent` | analyst, editor | Propose changes to the working dataset |
| `review-agent` | analyst, approver | Approve or reject the changes of other agents |

All passwords and secrets in this repository are placeholders. Do not use them outside the demo.

## Connect an agent

Refer to [docs/onboarding.md](docs/onboarding.md). Example for Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "titanic": {
      "type": "http",
      "url": "http://localhost:18080/mcp",
      "headers": {"Authorization": "Bearer ${TITANIC_MCP_TOKEN}"}
    }
  }
}
```

## Repository layout

```text
src/tmcp/            server code (auth, policy, query guard, db adapters, tools, observability)
tests/               unit and integration tests (185 tests)
policy/policy.yaml   access policy (roles and agents)
charts/titanic-mcp/  Helm chart (Deployment, HPA, PDB, NetworkPolicy, ServiceMonitor, alerts, dashboard)
deploy/kind/         demo cluster: kind config, Keycloak, PostgreSQL, Jaeger, monitoring values
deploy/compose/      local environment with Docker Compose
dashboards/          Grafana dashboard (JSON)
docs/                specification, onboarding guide, runbooks, architecture decisions
.github/workflows/   CI and release pipelines (not run for the demo)
```

## Configuration

The server reads all settings from `TMCP_*` environment variables. Refer to `src/tmcp/config.py` and the
[specification, section 10](docs/specification.md#10-configuration-and-secrets). Important values:

| Variable | Function |
| --- | --- |
| `TMCP_ENV` | `development`, `demo`, `staging`, `production`. API keys are refused in staging and production. |
| `TMCP_AUTH_MODE` | `jwt` or `api_key` |
| `TMCP_JWT_ISSUER`, `TMCP_JWKS_URL`, `TMCP_RESOURCE_URL` | IdP issuer, key set URL, URL of this server (token audience) |
| `TMCP_WORKING_DB_URL` | PostgreSQL URL of the working dataset (from a Secret). Not set = no working dataset. |
| `TMCP_POLICY_PATH` | Policy file. The server loads changes without a restart. |
| `TMCP_OTEL_ENDPOINT` | OTLP endpoint for traces |

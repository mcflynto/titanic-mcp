"""MCP resources and prompts (spec section 5)."""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from tmcp.db.catalog import CATALOG
from tmcp.errors import AppError
from tmcp.observability import metrics
from tmcp.observability.audit import audit_event
from tmcp.policy.engine import Permissions
from tmcp.tools.runtime import current_permissions, state

PERMISSION_DENIED_CODE = -32001

DATASET_INFO: dict[str, Any] = {
    "name": "Titanic passengers",
    "source": "https://github.com/davidjamesknight/SQLite_databases_for_learning_data_science/blob/main/titanic.db",
    "source_commit": "867f5bae5c0b53b0035bacb671ae1d3b07bc18ce",
    "content": "891 passengers of the RMS Titanic (1912). No names and no personal identifiers.",
    "datasets": {
        "reference": "The original file, read-only. It never changes.",
        "working": "A writable PostgreSQL copy. It changes only through approved change requests.",
    },
    "data_quality": {
        "age_null_rows": 177,
        "deck_unknown_rows": 688,
        "embarked_unknown_rows": 2,
        "unknown_code": "The raw tables use -1 for unknown. The table 'passengers' shows NULL.",
    },
}


def _governed_resource(
    uri: str, needs_tool: str | None, build: Callable[[Permissions], Awaitable[Any]]
) -> Callable[[], Awaitable[str]]:
    async def read() -> str:
        start = time.perf_counter()
        perms: Permissions | None = None
        decision, reason = "allow", None
        try:
            perms = current_permissions()
            if needs_tool and not perms.can(needs_tool):
                decision, reason = "deny", f"needs permission for {needs_tool}"
                metrics.PERMISSION_DENIED.labels(
                    metrics.agent_label(perms.agent_id, state().policy.known_agents), uri
                ).inc()
                raise MCPError(code=PERMISSION_DENIED_CODE, message=f"PERMISSION_DENIED: you cannot read {uri}.")
            return json.dumps(await build(perms), indent=2, default=str)
        except AppError as exc:
            decision, reason = "deny", exc.message
            raise MCPError(code=PERMISSION_DENIED_CODE, message=f"{exc.code.value}: {exc.message}") from exc
        finally:
            audit_event(
                agent_id=perms.agent_id if perms else None,
                roles=list(perms.roles) if perms else [],
                tool=f"resource:{uri}",
                arguments={},
                decision=decision,
                deny_reason=reason,
                duration_ms=(time.perf_counter() - start) * 1000,
            )

    return read


# Resources and the tool permission that they need (MCP-03 also filters resources/list).
RESOURCE_PERMISSIONS: dict[str, str | None] = {
    "titanic://schema": "list_tables",
    "titanic://dataset/info": "list_tables",
    "titanic://me": None,
}


async def _schema(perms: Permissions) -> Any:
    out: dict[str, Any] = {}
    for dataset in CATALOG:
        if dataset == "working" and state().working is None:
            continue
        tables = {}
        for table in perms.visible_tables(dataset):
            access = perms.table(dataset, table.name)
            if access is None:
                continue
            tables[table.name] = {
                "description": table.description,
                "columns": [
                    {"name": c.name, "type": c.type, "description": c.description, "nullable": c.nullable}
                    for c in table.columns
                    if c.name in access.columns
                ],
            }
        out[dataset] = tables
    return out


async def _info(_: Permissions) -> Any:
    return DATASET_INFO | {"reference_sha256": state().reference.sha256, "server_version": state().version}


async def _me(perms: Permissions) -> Any:
    return {
        "agent_id": perms.agent_id,
        "roles": list(perms.roles),
        "tools": sorted(perms.tools),
        "tables": {
            f"{dataset}.{name}": {
                "columns": sorted(access.columns),
                "row_filter": access.row_filter.sql() if access.row_filter is not None else None,
            }
            for (dataset, name), access in sorted(perms.tables.items())
        },
        "limits": perms.limits.model_dump(),
        "min_group_size": perms.min_group_size,
        "policy_version": state().policy.version,
    }


def register(server: MCPServer) -> None:
    server.resource(
        "titanic://schema", name="schema", title="Database schema", mime_type="application/json",
        description="Tables and columns that you can read, in all datasets.",
    )(_governed_resource("titanic://schema", "list_tables", _schema))  # fmt: skip
    server.resource(
        "titanic://dataset/info", name="dataset-info", title="Dataset information", mime_type="application/json",
        description="Source, content, data quality facts and checksum of the data.",
    )(_governed_resource("titanic://dataset/info", "list_tables", _info))  # fmt: skip
    server.resource(
        "titanic://me", name="me", title="My permissions", mime_type="application/json",
        description="Your agent ID, roles, tools, tables, columns, row filters and limits.",
    )(_governed_resource("titanic://me", None, _me))  # fmt: skip

    @server.prompt(name="analyze_survival", title="Analyze survival")
    def analyze_survival(dimensions: str = "class, sex") -> str:
        """A template to analyze the survival rate by one or more dimensions."""
        return (
            f"Analyze the survival of the Titanic passengers by these dimensions: {dimensions}.\n"
            "1. Read the resource titanic://me to see your permissions.\n"
            "2. Call describe_table for the table 'passengers' to see the permitted values.\n"
            f"3. Call aggregate_passengers with group_by = [{dimensions}] and the metric "
            "{'fn': 'avg', 'column': 'survived'}. The result 'avg_survived' is the survival rate.\n"
            "4. Report the survival rate and the group_size of each group. Do not report groups that the "
            "server removed. Say which differences are large."
        )

"""Policy file schema (spec section 6, PERM-01). The server validates the file at start and at each reload."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tmcp.db.catalog import CATALOG, Dataset

READ_TOOLS = (
    "list_tables",
    "describe_table",
    "get_sample_rows",
    "query_passengers",
    "aggregate_passengers",
    "run_sql",
)
WRITE_TOOLS = (
    "propose_passenger_insert",
    "propose_passenger_update",
    "propose_passenger_delete",
    "list_change_requests",
    "get_change_request",
    "review_change_request",
)
KNOWN_TOOLS = frozenset(READ_TOOLS + WRITE_TOOLS)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnRule(_Strict):
    allow: list[str] | None = None
    deny: list[str] = Field(default_factory=list)


class TableRule(_Strict):
    columns: ColumnRule = Field(default_factory=ColumnRule)
    row_filter: str | None = None


class Limits(_Strict):
    max_rows: int = Field(100, ge=1, le=10_000)
    requests_per_minute: int = Field(60, ge=1)
    max_concurrency: int = Field(4, ge=1)
    query_timeout_s: float = Field(5.0, gt=0, le=60)


class Role(_Strict):
    description: str = ""
    tools: list[str]
    datasets: dict[Dataset, dict[str, TableRule]] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    min_group_size: int | None = Field(None, ge=1)


class Agent(_Strict):
    description: str = ""
    roles: list[str]


class Policy(_Strict):
    version: Literal[1]
    roles: dict[str, Role]
    agents: dict[str, Agent] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        from tmcp.query.guard import parse_row_filter

        for role_name, role in self.roles.items():
            unknown_tools = set(role.tools) - KNOWN_TOOLS
            if unknown_tools:
                raise ValueError(f"Role '{role_name}': unknown tools {sorted(unknown_tools)}")
            for dataset, tables in role.datasets.items():
                for table_name, rule in tables.items():
                    targets = (
                        list(CATALOG[dataset].values())
                        if table_name == "*"
                        else [CATALOG[dataset].get(table_name.lower())]
                    )
                    for table in targets:
                        if table is None:
                            raise ValueError(f"Role '{role_name}': unknown table '{dataset}.{table_name}'")
                        named = set(rule.columns.deny) | set(rule.columns.allow or [])
                        if table_name != "*" and named - table.column_names:
                            raise ValueError(
                                f"Role '{role_name}': unknown columns {sorted(named - table.column_names)} "
                                f"in '{dataset}.{table.name}'"
                            )
                        if rule.row_filter:
                            parse_row_filter(rule.row_filter, table)
        for agent_name, agent in self.agents.items():
            missing = set(agent.roles) - set(self.roles)
            if missing:
                raise ValueError(f"Agent '{agent_name}': unknown roles {sorted(missing)}")
        return self

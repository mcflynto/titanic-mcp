"""Effective permissions of an agent (PERM-03, PERM-04). Deny by default; several roles give the union."""

from dataclasses import dataclass, field

from sqlglot import exp

from tmcp.db.catalog import CATALOG, Dataset, Table
from tmcp.policy.models import Limits, Policy, Role, TableRule
from tmcp.query.guard import parse_row_filter


@dataclass(frozen=True)
class TableAccess:
    columns: frozenset[str]
    row_filter: exp.Expr | None


@dataclass(frozen=True)
class Permissions:
    agent_id: str
    roles: tuple[str, ...]
    tools: frozenset[str]
    tables: dict[tuple[Dataset, str], TableAccess] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    min_group_size: int | None = None

    def can(self, tool: str) -> bool:
        return tool in self.tools

    def table(self, dataset: Dataset, name: str) -> TableAccess | None:
        return self.tables.get((dataset, name.lower()))

    def visible_tables(self, dataset: Dataset) -> list[Table]:
        return [t for key, t in CATALOG[dataset].items() if (dataset, key) in self.tables]


def _columns(table: Table, rule: TableRule) -> frozenset[str]:
    allowed = set(rule.columns.allow) if rule.columns.allow is not None else set(table.column_names)
    return frozenset((allowed & table.column_names) - set(rule.columns.deny))


def _role_tables(role: Role) -> dict[tuple[Dataset, str], TableAccess]:
    result: dict[tuple[Dataset, str], TableAccess] = {}
    for dataset, rules in role.datasets.items():
        for name, rule in rules.items():
            tables = list(CATALOG[dataset].values()) if name == "*" else [CATALOG[dataset][name.lower()]]
            for table in tables:
                row_filter = parse_row_filter(rule.row_filter, table) if rule.row_filter else None
                result[(dataset, table.name.lower())] = TableAccess(_columns(table, rule), row_filter)
    return result


def _merge(a: TableAccess, b: TableAccess) -> TableAccess:
    if a.row_filter is None or b.row_filter is None:
        row_filter = None  # one role without a filter gives all rows
    else:
        row_filter = exp.or_(a.row_filter.copy(), b.row_filter.copy())
    return TableAccess(a.columns | b.columns, row_filter)


def resolve(policy: Policy, agent_id: str, token_roles: list[str] | None = None) -> Permissions:
    role_names = list(policy.agents[agent_id].roles) if agent_id in policy.agents else []
    for name in token_roles or []:
        if name in policy.roles and name not in role_names:
            role_names.append(name)
    roles = [policy.roles[n] for n in role_names]
    if not roles:
        return Permissions(agent_id=agent_id, roles=(), tools=frozenset())

    tables: dict[tuple[Dataset, str], TableAccess] = {}
    for role in roles:
        for key, access in _role_tables(role).items():
            tables[key] = _merge(tables[key], access) if key in tables else access

    limits = Limits(
        max_rows=max(r.limits.max_rows for r in roles),
        requests_per_minute=max(r.limits.requests_per_minute for r in roles),
        max_concurrency=max(r.limits.max_concurrency for r in roles),
        query_timeout_s=max(r.limits.query_timeout_s for r in roles),
    )
    aggregators = [r for r in roles if "aggregate_passengers" in r.tools]
    min_group: int | None = None
    if aggregators and all(r.min_group_size is not None for r in aggregators):
        min_group = min(r.min_group_size for r in aggregators if r.min_group_size is not None)

    return Permissions(
        agent_id=agent_id,
        roles=tuple(role_names),
        tools=frozenset(t for r in roles for t in r.tools),
        tables=tables,
        limits=limits,
        min_group_size=min_group,
    )

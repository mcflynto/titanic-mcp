"""Read-only tools (spec section 5)."""

import json
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from tmcp.db.catalog import Dataset, Table, get_table
from tmcp.errors import AppError, ErrorCode, denied, invalid
from tmcp.observability.context import current_request_id
from tmcp.policy.engine import Permissions, TableAccess
from tmcp.query.builder import Filter, Metric, OrderBy, build_aggregate, build_count, build_select
from tmcp.query.guard import guard_sql
from tmcp.tools.models import (
    ColumnInfo,
    DescribeTableResult,
    ListTablesResult,
    RowsResult,
    TableInfo,
)
from tmcp.tools.runtime import governed_tool, state

DatasetArg = Annotated[
    Dataset,
    Field(description="'reference' = original read-only data. 'working' = writable copy with approved changes."),
]


def _resolve_table(perms: Permissions, dataset: Dataset, name: str) -> tuple[Table, TableAccess]:
    table = get_table(dataset, name)
    if table is None:
        raise invalid(f"The table '{name}' does not exist in the dataset '{dataset}'.", "Use the tool list_tables.")
    access = perms.table(dataset, table.name)
    if access is None:
        raise denied(f"You have no permission for '{dataset}.{table.name}'.", "Use the tool list_tables.")
    return table, access


def _check_result_size(rows: list[list[Any]]) -> None:
    limit = state().settings.max_result_bytes
    if len(json.dumps(rows, default=str)) > limit:
        raise AppError(
            ErrorCode.RESULT_TOO_LARGE,
            f"The result is larger than {limit} bytes.",
            hint="Select fewer columns or rows, or aggregate the data.",
        )


def _round_floats(rows: list[list[Any]]) -> list[list[Any]]:
    return [[round(v, 6) if isinstance(v, float) else v for v in row] for row in rows]


def register(server: MCPServer) -> None:
    @governed_tool(server, name="list_tables", title="List tables", output=ListTablesResult)
    async def list_tables(perms: Permissions) -> ListTablesResult:
        """Show the tables that you can read, in the datasets 'reference' and 'working'.
        Start with this tool. For most questions, use the table 'passengers'."""
        tables = [
            TableInfo(
                dataset=dataset,
                name=t.name,
                description=t.description,
                is_view=t.is_view,
                columns=[c.name for c in t.columns if c.name in (perms.table(dataset, t.name) or _EMPTY).columns],
            )
            for dataset in ("reference", "working")
            for t in perms.visible_tables(dataset)
            if dataset == "reference" or state().working is not None
        ]
        return ListTablesResult(tables=tables)

    @governed_tool(server, name="describe_table", title="Describe a table", output=DescribeTableResult)
    async def describe_table(
        perms: Permissions,
        table: Annotated[str, Field(description="Table name, for example 'passengers'.")],
        dataset: DatasetArg = "reference",
    ) -> DescribeTableResult:
        """Show the columns, data types, permitted values, row count and NULL counts of one table.
        Columns that your role cannot see are listed in 'masked_columns'."""
        tbl, access = _resolve_table(perms, dataset, table)
        db = state().database(dataset)
        built = build_count(tbl, access, db.dialect)
        result = await db.fetch(built.sql, built.params, max_rows=1, timeout_s=perms.limits.query_timeout_s)
        stats = dict(zip(result.columns, result.rows[0], strict=True))
        columns = [
            ColumnInfo(
                name=c.name,
                type=c.type,
                description=c.description,
                nullable=c.nullable,
                values=list(c.values) if c.values else None,
                null_count=int(stats.get(f"null_{c.name}") or 0),
            )
            for c in tbl.columns
            if c.name in access.columns
        ]
        return DescribeTableResult(
            dataset=dataset,
            table=tbl.name,
            description=tbl.description,
            row_count=int(stats["row_count"]),
            columns=columns,
            masked_columns=[c.name for c in tbl.columns if c.name not in access.columns],
            row_filter_applied=access.row_filter is not None,
        )

    @governed_tool(server, name="get_sample_rows", title="Get sample rows", output=RowsResult)
    async def get_sample_rows(
        perms: Permissions,
        table: Annotated[str, Field(description="Table name, for example 'passengers'.")],
        dataset: DatasetArg = "reference",
        limit: Annotated[int, Field(ge=1, le=20, description="Number of rows (1 to 20).")] = 5,
    ) -> RowsResult:
        """Show the first rows of one table, so that you can see the data format."""
        tbl, access = _resolve_table(perms, dataset, table)
        db = state().database(dataset)
        built = build_select(
            tbl, access, columns=None, filters=[], order_by=[], limit=limit, offset=0, dialect=db.dialect
        )
        result = await db.fetch(built.sql, built.params, max_rows=limit, timeout_s=perms.limits.query_timeout_s)
        return RowsResult(
            dataset=dataset,
            columns=result.columns,
            rows=_round_floats(result.rows),
            row_count=len(result.rows),
            truncated=False,
            masked_columns=built.masked_columns,
            request_id=current_request_id(),
        )

    @governed_tool(server, name="query_passengers", title="Query passengers", output=RowsResult)
    async def query_passengers(
        perms: Permissions,
        dataset: DatasetArg = "reference",
        columns: Annotated[
            list[str] | None, Field(description="Columns to return. Empty = all columns that you can see.")
        ] = None,
        filters: Annotated[list[Filter] | None, Field(description="Conditions, combined with AND.")] = None,
        order_by: Annotated[list[OrderBy] | None, Field(description="Sort order.")] = None,
        limit: Annotated[int, Field(ge=1, le=10_000, description="Maximum rows. Your role can reduce it.")] = 100,
        offset: Annotated[int, Field(ge=0, description="Rows to skip, for pages.")] = 0,
    ) -> RowsResult:
        """Find passenger rows with structured filters. No SQL is necessary.
        Example filter: {"column": "age", "op": "<", "value": 10}.
        If 'truncated' is true, more rows exist: use 'offset' or add filters."""
        tbl, access = _resolve_table(perms, dataset, "passengers")
        db = state().database(dataset)
        max_rows = min(limit, perms.limits.max_rows)
        built = build_select(
            tbl,
            access,
            columns=columns,
            filters=filters or [],
            order_by=order_by or [],
            limit=max_rows + 1,
            offset=offset,
            dialect=db.dialect,
        )
        result = await db.fetch(built.sql, built.params, max_rows=max_rows, timeout_s=perms.limits.query_timeout_s)
        _check_result_size(result.rows)
        return RowsResult(
            dataset=dataset,
            columns=result.columns,
            rows=_round_floats(result.rows),
            row_count=len(result.rows),
            truncated=result.truncated,
            masked_columns=built.masked_columns,
            request_id=current_request_id(),
        )

    @governed_tool(server, name="aggregate_passengers", title="Aggregate passengers", output=RowsResult)
    async def aggregate_passengers(
        perms: Permissions,
        group_by: Annotated[list[str], Field(description="Columns to group by, for example ['class', 'sex'].")],
        metrics: Annotated[
            list[Metric] | None,
            Field(description="Statistics. Example: [{'fn': 'avg', 'column': 'survived'}] = survival rate."),
        ] = None,
        filters: Annotated[list[Filter] | None, Field(description="Conditions, combined with AND.")] = None,
        dataset: DatasetArg = "reference",
    ) -> RowsResult:
        """Calculate statistics by groups, for example the survival rate by class and sex.
        Each result row has 'group_size' (number of passengers). Some roles see only groups with
        a minimum size; smaller groups are removed."""
        tbl, access = _resolve_table(perms, dataset, "passengers")
        db = state().database(dataset)
        built = build_aggregate(
            tbl,
            access,
            group_by=group_by,
            metrics=metrics or [],
            filters=filters or [],
            min_group_size=perms.min_group_size,
            dialect=db.dialect,
        )
        max_rows = perms.limits.max_rows
        result = await db.fetch(built.sql, built.params, max_rows=max_rows, timeout_s=perms.limits.query_timeout_s)
        return RowsResult(
            dataset=dataset,
            columns=result.columns,
            rows=_round_floats(result.rows),
            row_count=len(result.rows),
            truncated=result.truncated,
            request_id=current_request_id(),
        )

    @governed_tool(server, name="run_sql", title="Run a read-only SQL query", output=RowsResult)
    async def run_sql(
        perms: Permissions,
        sql: Annotated[str, Field(description="One SQLite SELECT statement on the reference dataset.")],
        limit: Annotated[
            int | None, Field(ge=1, le=10_000, description="Maximum rows. Your role can reduce it.")
        ] = None,
    ) -> RowsResult:
        """Run one read-only SELECT statement (SQLite syntax) on the reference dataset.
        Tables: passengers (recommended), Observation and the lookup tables. Only SELECT and WITH are
        permitted. The server adds a LIMIT and applies your column and row rules."""
        settings = state().settings
        max_rows = min(limit or perms.limits.max_rows, perms.limits.max_rows)
        guarded = guard_sql(
            sql, dataset="reference", perms=perms, max_rows=max_rows, max_length=settings.max_sql_length
        )
        result = await state().reference.fetch(
            guarded.sql, {}, max_rows=max_rows, timeout_s=perms.limits.query_timeout_s
        )
        _check_result_size(result.rows)
        return RowsResult(
            dataset="reference",
            columns=result.columns,
            rows=_round_floats(result.rows),
            row_count=len(result.rows),
            truncated=result.truncated,
            request_id=current_request_id(),
        )


_EMPTY = TableAccess(frozenset(), None)

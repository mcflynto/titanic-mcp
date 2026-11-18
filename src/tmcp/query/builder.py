"""SQL for the structured tools. Agent values go only into bound parameters (QS-07)."""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp

from tmcp.db.catalog import Column, Table
from tmcp.errors import denied, invalid
from tmcp.policy.engine import TableAccess

FilterOp = Literal["=", "!=", "<", "<=", ">", ">=", "in", "not_in", "between", "is_null", "like"]
AggregateFn = Literal["count", "sum", "avg", "min", "max"]


class Filter(BaseModel):
    """One condition. Several filters are combined with AND."""

    model_config = ConfigDict(extra="forbid")
    column: str = Field(description="Column name, for example 'sex' or 'age'.")
    op: FilterOp = Field(description="Operator. 'in'/'not_in' need a list, 'between' needs [low, high], "
                                     "'is_null' needs true or false, 'like' needs a pattern with %.")  # fmt: skip
    value: Any = Field(None, description="Comparison value.")


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str
    direction: Literal["asc", "desc"] = "asc"


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fn: AggregateFn = Field(description="Aggregate function. Use 'avg' of 'survived' for the survival rate.")
    column: str | None = Field(None, description="Column. Leave empty with 'count' to count rows.")


@dataclass
class BuiltQuery:
    sql: str
    params: dict[str, Any]
    masked_columns: list[str] = field(default_factory=list)


class _Params:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def add(self, value: Any) -> exp.Placeholder:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return exp.Placeholder(this=name)


def _col(name: str) -> exp.Column:
    return exp.column(name, quoted=True)


def _check_column(table: Table, access: TableAccess, name: str) -> Column:
    column = table.column(name)
    if column is None:
        raise invalid(f"The column '{name}' does not exist in '{table.name}'.", "Use the tool describe_table.")
    if name not in access.columns:
        raise denied(f"You have no permission for the column '{name}'.", "Read the resource titanic://me.")
    return column


def _check_scalar(column: Column, value: Any) -> None:
    if value is None or isinstance(value, bool):
        raise invalid(f"The value for '{column.name}' must be a {column.type}, not {value!r}.")
    if column.type in ("integer", "real") and not isinstance(value, int | float):
        raise invalid(f"The value for '{column.name}' must be a number.")
    if column.type == "text" and not isinstance(value, str):
        raise invalid(f"The value for '{column.name}' must be text.")


def _condition(table: Table, access: TableAccess, f: Filter, params: _Params) -> exp.Expr:
    column = _check_column(table, access, f.column)
    target = _col(f.column)
    if f.op == "is_null":
        if not isinstance(f.value, bool):
            raise invalid("The operator 'is_null' needs the value true or false.")
        return target.is_(exp.null()) if f.value else exp.not_(target.is_(exp.null()))
    if f.op in ("in", "not_in"):
        if not isinstance(f.value, list) or not 1 <= len(f.value) <= 100:
            raise invalid(f"The operator '{f.op}' needs a list with 1 to 100 values.")
        for v in f.value:
            _check_scalar(column, v)
        node = target.isin(*[params.add(v) for v in f.value])
        return exp.not_(node) if f.op == "not_in" else node
    if f.op == "between":
        if not isinstance(f.value, list) or len(f.value) != 2:
            raise invalid("The operator 'between' needs a list [low, high].")
        for v in f.value:
            _check_scalar(column, v)
        return target.between(params.add(f.value[0]), params.add(f.value[1]))
    if f.op == "like":
        if column.type != "text" or not isinstance(f.value, str):
            raise invalid("The operator 'like' needs a text column and a text pattern.")
        return target.like(params.add(f.value))
    _check_scalar(column, f.value)
    ops = {"=": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}
    return ops[f.op](this=target, expression=params.add(f.value))


def _where(table: Table, access: TableAccess, filters: list[Filter], params: _Params) -> exp.Expr | None:
    conditions = [_condition(table, access, f, params) for f in filters]
    if access.row_filter is not None:
        conditions.append(exp.paren(access.row_filter.copy()))
    return exp.and_(*conditions) if conditions else None


def build_select(
    table: Table,
    access: TableAccess,
    *,
    columns: list[str] | None,
    filters: list[Filter],
    order_by: list[OrderBy],
    limit: int,
    offset: int,
    dialect: str,
) -> BuiltQuery:
    params = _Params()
    if columns:
        selected = [_check_column(table, access, c).name for c in columns]
        masked: list[str] = []
    else:
        selected = [c.name for c in table.columns if c.name in access.columns]
        masked = [c.name for c in table.columns if c.name not in access.columns]
    query = exp.select(*[_col(c) for c in selected]).from_(exp.to_table(table.name, quoted=True))
    where = _where(table, access, filters, params)
    if where is not None:
        query = query.where(where)
    for o in order_by:
        _check_column(table, access, o.column)
        query = query.order_by(exp.Ordered(this=_col(o.column), desc=o.direction == "desc"))
    if not order_by and "passenger_id" in selected:
        query = query.order_by(_col("passenger_id"))
    query = query.limit(params.add(limit)).offset(params.add(offset))
    return BuiltQuery(query.sql(dialect=dialect), params.values, masked)


def build_count(table: Table, access: TableAccess, dialect: str) -> BuiltQuery:
    """Row count and NULL count for each permitted column (describe_table)."""
    params = _Params()
    names = [c.name for c in table.columns if c.name in access.columns]
    projections: list[exp.Expr] = [exp.alias_(exp.Count(this=exp.Star()), "row_count")]
    for name in names:
        is_null = exp.Case().when(_col(name).is_(exp.null()), exp.Literal.number(1)).else_(exp.Literal.number(0))
        projections.append(exp.alias_(exp.Sum(this=is_null), f"null_{name}", quoted=True))
    query = exp.select(*projections).from_(exp.to_table(table.name, quoted=True))
    where = _where(table, access, [], params)
    if where is not None:
        query = query.where(where)
    return BuiltQuery(query.sql(dialect=dialect), params.values)


def build_aggregate(
    table: Table,
    access: TableAccess,
    *,
    group_by: list[str],
    metrics: list[Metric],
    filters: list[Filter],
    min_group_size: int | None,
    dialect: str,
) -> BuiltQuery:
    params = _Params()
    if len(group_by) > 4:
        raise invalid("Use 4 or fewer group_by columns.")
    if not metrics:
        metrics = [Metric(fn="count")]
    if len(metrics) > 10:
        raise invalid("Use 10 or fewer metrics.")
    group_cols = [_check_column(table, access, c).name for c in group_by]
    if "passenger_id" in group_cols:
        raise invalid("Grouping by passenger_id is not permitted. Use query_passengers for single rows.")

    projections: list[exp.Expr] = [_col(c) for c in group_cols]
    projections.append(exp.alias_(exp.Count(this=exp.Star()), "group_size"))
    seen: set[str] = {"group_size"}
    for m in metrics:
        if m.column is None:
            if m.fn != "count":
                raise invalid(f"The metric '{m.fn}' needs a column.")
            continue  # group_size already counts the rows
        column = _check_column(table, access, m.column)
        if m.fn in ("sum", "avg") and column.type == "text":
            raise invalid(f"The metric '{m.fn}' needs a numeric column, but '{column.name}' is text.")
        alias = f"{m.fn}_{column.name}"
        if alias in seen:
            continue
        seen.add(alias)
        fn_node = {"count": exp.Count, "sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[m.fn]
        projections.append(exp.alias_(fn_node(this=_col(column.name)), alias, quoted=True))

    query = exp.select(*projections).from_(exp.to_table(table.name, quoted=True))
    where = _where(table, access, filters, params)
    if where is not None:
        query = query.where(where)
    if group_cols:
        query = query.group_by(*[_col(c) for c in group_cols]).order_by(*[_col(c) for c in group_cols])
    if min_group_size:
        # k-anonymity: hide groups that are too small (role 'statistician').
        query = query.having(exp.GTE(this=exp.Count(this=exp.Star()), expression=params.add(min_group_size)))
    return BuiltQuery(query.sql(dialect=dialect), params.values)

"""SQL validation and rewrite with a syntax tree (QS-01 to QS-06, PERM-05).

The guard accepts one SELECT statement. It rejects all other statement types, unknown functions,
qualified names and system tables. It replaces each restricted table with a filtered subquery, so that
column and row rules apply also to free SQL. Then it sets a LIMIT.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from tmcp.db.catalog import Dataset, Table, get_table
from tmcp.errors import AppError, ErrorCode, denied, rejected
from tmcp.observability import metrics

if TYPE_CHECKING:
    from tmcp.policy.engine import Permissions, TableAccess

ALLOWED_FUNCTIONS = frozenset(
    {
        "COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL", "ROUND", "ABS", "COALESCE", "IFNULL", "NULLIF",
        "LOWER", "UPPER", "LENGTH", "SUBSTRING", "SUBSTR", "TRIM", "LTRIM", "RTRIM", "REPLACE", "INSTR",
        "CAST", "IF", "IIF", "GROUP_CONCAT", "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD",
        "FIRST_VALUE", "LAST_VALUE", "CASE", "ANY_VALUE", "STDDEV", "VARIANCE", "MEDIAN",
    }
)  # fmt: skip

# QS-02: statement and clause types that are never permitted.
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "TruncateTable",
    "Command", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback", "Set", "Use",
    "Copy", "Grant", "Into", "LoadData", "Analyze",
)  # fmt: skip
_FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = tuple(
    t for t in (getattr(exp, n, None) for n in _FORBIDDEN_NODE_NAMES) if isinstance(t, type)
)
_ROW_FILTER_FORBIDDEN: tuple[type[exp.Expr], ...] = (exp.Query, exp.Subquery, exp.Table, exp.Placeholder)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    return node.sql_name().upper()


def _reject(reason: str, message: str, hint: str | None = None) -> AppError:
    metrics.QUERY_REJECTED.labels(reason).inc()
    return rejected(message, hint)


def check_functions(tree: exp.Expr) -> None:
    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if name not in ALLOWED_FUNCTIONS:
            raise _reject("function", f"The function '{name.lower()}' is not permitted.")


def parse_row_filter(text: str, table: Table) -> exp.Expr:
    """Validate a policy row filter. It may use only columns of the table and permitted functions."""
    try:
        node = sqlglot.parse_one(text, read="sqlite")
    except ParseError as exc:
        raise ValueError(f"Row filter '{text}' is not valid SQL: {exc}") from exc
    if any(isinstance(n, _ROW_FILTER_FORBIDDEN) for n in node.walk()):
        raise ValueError(f"Row filter '{text}' must not contain subqueries, tables or parameters")
    for column in node.find_all(exp.Column):
        if column.table or column.name not in table.column_names:
            raise ValueError(f"Row filter '{text}' uses an unknown column '{column.sql()}'")
    try:
        check_functions(node)
    except AppError as exc:
        raise ValueError(f"Row filter '{text}': {exc.message}") from exc
    return node


def _require_access(perms: "Permissions", dataset: Dataset, table: Table) -> "TableAccess":
    access = perms.table(dataset, table.name)
    if access is None:  # not reachable: the table was checked before; never trust an assert here
        raise denied(f"You have no permission for the table '{table.name}'.")
    return access


@dataclass
class GuardedQuery:
    sql: str
    tables: list[str]


def _restricted_source(table: Table, columns: frozenset[str], row_filter: exp.Expr | None) -> exp.Select:
    ordered = [c.name for c in table.columns if c.name in columns]
    select = exp.select(*[exp.column(c, quoted=True) for c in ordered]).from_(exp.to_table(table.name, quoted=True))
    if row_filter is not None:
        select = select.where(row_filter.copy())
    return select


def guard_sql(sql: str, *, dataset: Dataset, perms: "Permissions", max_rows: int, max_length: int) -> GuardedQuery:
    if len(sql) > max_length:
        raise _reject("length", f"The SQL text is longer than {max_length} characters.")
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except ParseError as exc:
        raise _reject("syntax", f"The SQL text is not valid: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise _reject("multiple_statements", "Send exactly one SQL statement.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise _reject(
            "not_select",
            "Only SELECT statements are permitted.",
            "The working data changes only through change requests.",
        )
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise _reject("forbidden_clause", f"The clause '{node.key.upper()}' is not permitted.")
        if isinstance(node, exp.Placeholder | exp.Parameter) or (
            isinstance(node, exp.Identifier) and not node.quoted and str(node.this)[:1] in ("$", ":", "@", "?")
        ):
            raise _reject("parameter", "SQL parameters are not permitted. Write the values into the query.")
    check_functions(tree)

    cte_names = {str(cte.alias).lower() for cte in tree.find_all(exp.CTE)}
    for name in cte_names:
        if get_table(dataset, name) is not None or name.startswith("sqlite_"):
            raise _reject("cte_name", f"The CTE name '{name}' is the name of a table. Use a different name.")

    referenced: dict[str, Table] = {}
    to_rewrite: list[tuple[exp.Table, Table]] = []
    for node in list(tree.find_all(exp.Table)):
        if not isinstance(node.this, exp.Identifier):
            raise _reject("table_function", "Table-valued functions are not permitted.")
        if node.args.get("db") or node.args.get("catalog"):
            raise _reject("qualified_name", "Qualified table names (schema.table) are not permitted.")
        name = node.name.lower()
        if name in cte_names:
            continue
        if name.startswith("sqlite_"):
            raise _reject("system_table", f"The table '{node.name}' is not permitted.")
        table = get_table(dataset, name)
        if table is None:
            raise _reject("unknown_table", f"The table '{node.name}' does not exist.", "Use the tool list_tables.")
        access = perms.table(dataset, table.name)
        if access is None:
            metrics.QUERY_REJECTED.labels("table_permission").inc()
            raise denied(f"You have no permission for the table '{table.name}'.", "Use the tool list_tables.")
        referenced[table.name] = table
        if access.columns != table.column_names or access.row_filter is not None:
            to_rewrite.append((node, table))

    denied_columns: set[str] = set()
    for table in referenced.values():
        access = _require_access(perms, dataset, table)
        denied_columns |= set(table.column_names - access.columns)
    for column in tree.find_all(exp.Column):
        if column.name in denied_columns:
            metrics.QUERY_REJECTED.labels("column_permission").inc()
            raise denied(f"You have no permission for the column '{column.name}'.", "Read the resource titanic://me.")

    # PERM-05: replace restricted tables with filtered subqueries.
    for node, table in to_rewrite:
        access = _require_access(perms, dataset, table)
        alias = node.alias or table.name
        subquery = _restricted_source(table, access.columns, access.row_filter).subquery(alias)
        node.replace(subquery)

    # QS-04: set or reduce the LIMIT. The adapter reads max_rows + 1 rows to detect truncation.
    limit = tree.args.get("limit")
    if limit is not None:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit()):
            raise _reject("limit", "LIMIT must be a positive integer.")
        if int(value.this) > max_rows:
            tree = tree.limit(max_rows + 1, copy=False)
    else:
        tree = tree.limit(max_rows + 1, copy=False)

    return GuardedQuery(sql=tree.sql(dialect="sqlite"), tables=sorted(referenced))


def ensure(condition: bool, code: ErrorCode, message: str) -> None:
    if not condition:
        raise AppError(code, message)

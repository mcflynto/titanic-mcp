"""SQL guard tests (QS-01 to QS-09, PERM-05). QS-09 asks for 50 or more injection and bypass cases."""

import pytest

from tmcp.db.catalog import PASSENGERS_VIEW
from tmcp.errors import AppError, ErrorCode
from tmcp.policy.store import PolicyStore
from tmcp.query.guard import guard_sql, parse_row_filter

REJECTED_SQL = [
    # QS-01: one statement, SELECT only
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE Sex",
    "SELECT * FROM passengers; DELETE FROM Observation",
    "INSERT INTO Sex VALUES (9, 'x')",
    "UPDATE Observation SET age = 1",
    "DELETE FROM Observation",
    "REPLACE INTO Sex VALUES (1, 'x')",
    "CREATE TABLE x (a INT)",
    "CREATE TEMP TABLE x AS SELECT * FROM passengers",
    "CREATE VIEW v AS SELECT 1",
    "DROP TABLE Observation",
    "DROP VIEW passengers",
    "ALTER TABLE Sex ADD COLUMN y TEXT",
    "CREATE INDEX i ON Sex(sex)",
    "CREATE TRIGGER t AFTER INSERT ON Sex BEGIN SELECT 1; END",
    "WITH x AS (SELECT 1) DELETE FROM Sex",
    "WITH x AS (SELECT 1) INSERT INTO Sex SELECT * FROM x",
    # QS-02: forbidden statements and functions
    "PRAGMA table_info(Sex)",
    "PRAGMA query_only = OFF",
    "PRAGMA writable_schema = ON",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "ATTACH '/etc/passwd' AS p",
    "DETACH DATABASE main",
    "VACUUM",
    "VACUUM INTO '/tmp/copy.db'",
    "ANALYZE",
    "BEGIN TRANSACTION",
    "COMMIT",
    "SELECT load_extension('/tmp/evil.so')",
    "SELECT readfile('/etc/passwd')",
    "SELECT writefile('/tmp/x', 'y')",
    "SELECT randomblob(1000000000)",
    "SELECT zeroblob(1000000000)",
    "SELECT sqlite_version()",
    "SELECT fts3_tokenizer('simple')",
    "SELECT hex(randomblob(16))",
    "SELECT printf('%s', sex) FROM passengers",
    "SELECT likelihood(1, 0.5)",
    "SELECT * FROM passengers WHERE sex = (SELECT char(109,97,108,101))",
    # QS-03: system tables, qualified names, unknown tables, table functions
    "SELECT * FROM sqlite_master",
    "SELECT * FROM sqlite_schema",
    "SELECT * FROM sqlite_temp_master",
    "SELECT name FROM SQLITE_MASTER",
    "SELECT * FROM main.Observation",
    "SELECT * FROM temp.passengers",
    "SELECT * FROM main.sqlite_master",
    "SELECT * FROM pragma_table_info('Sex')",
    "SELECT * FROM unknown_table",
    "SELECT * FROM passengers UNION SELECT name, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1 FROM sqlite_master",
    "SELECT (SELECT sql FROM sqlite_master LIMIT 1)",
    "WITH m AS (SELECT * FROM sqlite_master) SELECT * FROM m",
    "WITH Sex AS (SELECT 1) SELECT * FROM Sex",
    # parameters and syntax
    "SELECT * FROM passengers WHERE age = ?",
    "SELECT * FROM passengers WHERE age = :age",
    "SELECT * FROM passengers WHERE age = $1",
    "SELECT * FROM",
    "SELEC 1",
    "SELECT * FROM passengers LIMIT (SELECT 5)",
    "SELECT * FROM passengers LIMIT -1",
]


@pytest.fixture(scope="module")
def sql_perms(policy_store: PolicyStore):
    return policy_store.permissions("research-agent")


def test_qs09_set_has_50_or_more_cases() -> None:
    assert len(REJECTED_SQL) >= 50


@pytest.mark.parametrize("sql", REJECTED_SQL)
def test_qs01_qs02_qs03_rejects_dangerous_sql(sql: str, sql_perms) -> None:
    with pytest.raises(AppError) as info:
        guard_sql(sql, dataset="reference", perms=sql_perms, max_rows=100, max_length=10_000)
    assert info.value.code in (ErrorCode.QUERY_REJECTED, ErrorCode.PERMISSION_DENIED)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM passengers",
        "select class, avg(survived) from passengers group by class",
        "WITH t AS (SELECT * FROM passengers WHERE age < 10) SELECT count(*) FROM t",
        "SELECT o.age, s.sex FROM Observation o JOIN Sex s ON s.sex_id = o.sex_id",
        "SELECT sex FROM passengers UNION SELECT sex FROM Sex",
        "SELECT CASE WHEN age < 18 THEN 'child' ELSE 'adult' END AS g, count(*) FROM passengers GROUP BY 1",
        "SELECT class, ROW_NUMBER() OVER (PARTITION BY class ORDER BY age) FROM passengers",
        "SELECT round(avg(fare), 2) FROM passengers;",
    ],
)
def test_qs01_accepts_valid_select(sql: str, sql_perms) -> None:
    guarded = guard_sql(sql, dataset="reference", perms=sql_perms, max_rows=100, max_length=10_000)
    assert "LIMIT" in guarded.sql.upper()


def test_qs04_adds_limit(sql_perms) -> None:
    guarded = guard_sql("SELECT * FROM passengers", dataset="reference", perms=sql_perms, max_rows=10, max_length=999)
    assert guarded.sql.endswith("LIMIT 11")


def test_qs04_reduces_large_limit(sql_perms) -> None:
    guarded = guard_sql(
        "SELECT * FROM passengers LIMIT 5000", dataset="reference", perms=sql_perms, max_rows=10, max_length=999
    )
    assert guarded.sql.endswith("LIMIT 11")


def test_qs04_keeps_small_limit(sql_perms) -> None:
    guarded = guard_sql(
        "SELECT * FROM passengers LIMIT 3", dataset="reference", perms=sql_perms, max_rows=10, max_length=999
    )
    assert guarded.sql.endswith("LIMIT 3")


def test_qs06_rejects_long_sql(sql_perms) -> None:
    with pytest.raises(AppError) as info:
        guard_sql("SELECT 1 " + " " * 100, dataset="reference", perms=sql_perms, max_rows=10, max_length=50)
    assert info.value.code == ErrorCode.QUERY_REJECTED


def test_perm05_masked_column_is_denied_in_sql(policy_store: PolicyStore) -> None:
    perms = policy_store.permissions("churn-agent")  # analyst: fare is masked; no run_sql, but guard must hold
    with pytest.raises(AppError) as info:
        guard_sql("SELECT fare FROM passengers", dataset="reference", perms=perms, max_rows=10, max_length=999)
    assert info.value.code == ErrorCode.PERMISSION_DENIED


def test_perm05_select_star_returns_only_permitted_columns(policy_store: PolicyStore) -> None:
    perms = policy_store.permissions("steerage-agent")
    guarded = guard_sql("SELECT * FROM passengers", dataset="reference", perms=perms, max_rows=10, max_length=999)
    assert '"fare"' not in guarded.sql
    assert "Third" in guarded.sql  # row filter is in the subquery


def test_perm05_table_without_permission_is_denied(policy_store: PolicyStore) -> None:
    perms = policy_store.permissions("churn-agent")
    with pytest.raises(AppError) as info:
        guard_sql("SELECT * FROM Observation", dataset="reference", perms=perms, max_rows=10, max_length=999)
    assert info.value.code == ErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    "text",
    ["(SELECT 1) = 1", "age > (SELECT 1)", "unknown_col = 1", "load_extension('x') = 1", "p.age > 1", "age >"],
)
def test_perm01_rejects_invalid_row_filter(text: str) -> None:
    with pytest.raises(ValueError):
        parse_row_filter(text, PASSENGERS_VIEW)

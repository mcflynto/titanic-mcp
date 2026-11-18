"""Structured query builder (QS-07, QS-08) and reference data (DS-01 to DS-04)."""

import pytest

from tests.conftest import DB_PATH
from tmcp.db.catalog import PASSENGERS_VIEW
from tmcp.db.reference import ReferenceDatabase
from tmcp.db.working import to_asyncpg
from tmcp.errors import AppError, ErrorCode
from tmcp.policy.engine import TableAccess
from tmcp.query.builder import Filter, Metric, build_aggregate, build_select
from tmcp.tools.models import PassengerValues

ALL = TableAccess(PASSENGERS_VIEW.column_names, None)
NO_FARE = TableAccess(PASSENGERS_VIEW.column_names - {"fare"}, None)
EVIL = "x'; DROP TABLE Observation; --"


def test_qs07_values_are_bound_parameters() -> None:
    built = build_select(
        PASSENGERS_VIEW, ALL, columns=None, filters=[Filter(column="sex", op="=", value=EVIL)],
        order_by=[], limit=10, offset=0, dialect="sqlite",
    )  # fmt: skip
    assert EVIL not in built.sql
    assert EVIL in built.params.values()


@pytest.mark.parametrize(
    "f",
    [
        Filter(column="age", op="in", value=1),
        Filter(column="age", op="between", value=[1]),
        Filter(column="age", op="is_null", value="yes"),
        Filter(column="age", op="like", value="1%"),
        Filter(column="age", op="=", value="ten"),
        Filter(column="sex", op="=", value=1),
        Filter(column="nope", op="=", value=1),
        Filter(column="age", op="=", value=True),
    ],
)
def test_qs08_invalid_filters(f: Filter) -> None:
    with pytest.raises(AppError) as info:
        build_select(PASSENGERS_VIEW, ALL, columns=None, filters=[f], order_by=[], limit=1, offset=0, dialect="sqlite")
    assert info.value.code == ErrorCode.INVALID_ARGUMENT


def test_qs08_operator_allow_list_is_enforced_by_schema() -> None:
    with pytest.raises(ValueError):
        Filter(column="age", op="; DROP", value=1)  # type: ignore[arg-type]


def test_masked_column_is_denied_in_filter() -> None:
    with pytest.raises(AppError) as info:
        build_select(
            PASSENGERS_VIEW, NO_FARE, columns=None, filters=[Filter(column="fare", op=">", value=1)],
            order_by=[], limit=1, offset=0, dialect="sqlite",
        )  # fmt: skip
    assert info.value.code == ErrorCode.PERMISSION_DENIED


def test_default_columns_report_masked_columns() -> None:
    built = build_select(
        PASSENGERS_VIEW, NO_FARE, columns=None, filters=[], order_by=[], limit=1, offset=0, dialect="sqlite"
    )
    assert built.masked_columns == ["fare"]


def test_aggregate_min_group_size_adds_having() -> None:
    built = build_aggregate(
        PASSENGERS_VIEW, ALL, group_by=["class"], metrics=[Metric(fn="avg", column="survived")],
        filters=[], min_group_size=5, dialect="sqlite",
    )  # fmt: skip
    assert "HAVING" in built.sql and 5 in built.params.values()


def test_aggregate_rejects_passenger_id_group() -> None:
    with pytest.raises(AppError):
        build_aggregate(
            PASSENGERS_VIEW,
            ALL,
            group_by=["passenger_id"],
            metrics=[],
            filters=[],
            min_group_size=None,
            dialect="sqlite",
        )


def test_to_asyncpg_converts_placeholders() -> None:
    sql, args = to_asyncpg("SELECT %(p0)s, %(p1)s, %(p0)s", {"p0": 1, "p1": "a"})
    assert sql == "SELECT $1, $2, $1" and args == [1, "a"]


def test_ds01_checksum_mismatch_stops_start() -> None:
    with pytest.raises(RuntimeError, match="DS-01"):
        ReferenceDatabase(DB_PATH, "0" * 64)


async def test_ds02_database_is_read_only(reference_db: ReferenceDatabase) -> None:
    with pytest.raises(AppError):
        await reference_db.fetch("DELETE FROM Sex", {}, max_rows=1, timeout_s=1)
    result = await reference_db.fetch("SELECT count(*) FROM Sex", {}, max_rows=1, timeout_s=1)
    assert result.rows == [[2]]


async def test_ds03_ds04_view_has_text_values_and_null_for_unknown(reference_db: ReferenceDatabase) -> None:
    result = await reference_db.fetch(
        "SELECT count(*), sum(deck IS NULL), sum(embarked IS NULL), sum(age IS NULL), sum(deck = '') FROM passengers",
        {}, max_rows=1, timeout_s=1,
    )  # fmt: skip
    assert result.rows == [[891, 688, 2, 177, 0]]


async def test_qs05_timeout_stops_long_query(reference_db: ReferenceDatabase) -> None:
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000000) SELECT count(*) FROM c"
    with pytest.raises(AppError) as info:
        await reference_db.fetch(slow, {}, max_rows=1, timeout_s=0.2)
    assert info.value.code == ErrorCode.QUERY_TIMEOUT


async def test_all_reference_rows_are_valid_passenger_values(reference_db: ReferenceDatabase) -> None:
    cols = "survived, pclass, sex, age, sibsp, parch, fare, embarked, deck, who, adult_male, alone"
    result = await reference_db.fetch(f"SELECT {cols} FROM passengers", {}, max_rows=1000, timeout_s=2)
    for row in result.rows:
        PassengerValues.model_validate(dict(zip(result.columns, row, strict=True)))

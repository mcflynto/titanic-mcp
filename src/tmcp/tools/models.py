"""Output models of the tools. The MCP SDK publishes them as ``outputSchema`` (MCP-04)."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tmcp.db.catalog import Dataset


class TableInfo(BaseModel):
    dataset: Dataset
    name: str
    description: str
    is_view: bool
    columns: list[str]


class ListTablesResult(BaseModel):
    tables: list[TableInfo]


class ColumnInfo(BaseModel):
    name: str
    type: str
    description: str
    nullable: bool
    values: list[str | int] | None = None
    null_count: int | None = None


class DescribeTableResult(BaseModel):
    dataset: Dataset
    table: str
    description: str
    row_count: int
    columns: list[ColumnInfo]
    masked_columns: list[str]
    row_filter_applied: bool


class RowsResult(BaseModel):
    dataset: Dataset
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    masked_columns: list[str] = Field(default_factory=list)
    request_id: str | None = None


# ----- write models (OP-01) ---------------------------------------------------------------------

Binary = Literal[0, 1]


class PassengerValues(BaseModel):
    """All writable columns of one passenger. The server derives class, embark_town and alive."""

    model_config = ConfigDict(extra="forbid")
    survived: Binary
    pclass: Literal[1, 2, 3]
    sex: Literal["female", "male"]
    age: float | None = Field(None, ge=0, lt=120)
    sibsp: int = Field(0, ge=0, le=20)
    parch: int = Field(0, ge=0, le=20)
    fare: float | None = Field(None, ge=0, le=10_000)
    embarked: Literal["C", "Q", "S"] | None = None
    deck: Literal["A", "B", "C", "D", "E", "F", "G"] | None = None
    who: Literal["child", "man", "woman"]
    adult_male: Binary
    alone: Binary

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.adult_male != (1 if self.who == "man" else 0):
            raise ValueError("adult_male must be 1 if and only if who is 'man'")
        if (self.who == "man" and self.sex != "male") or (self.who == "woman" and self.sex != "female"):
            raise ValueError("who and sex do not agree")
        if self.alone != (1 if self.sibsp + self.parch == 0 else 0):
            raise ValueError("alone must be 1 if and only if sibsp + parch is 0")
        return self


class PassengerChanges(BaseModel):
    """The columns to change. All other columns keep their values."""

    model_config = ConfigDict(extra="forbid")
    survived: Binary | None = None
    pclass: Literal[1, 2, 3] | None = None
    sex: Literal["female", "male"] | None = None
    age: float | None = Field(None, ge=0, lt=120)
    sibsp: int | None = Field(None, ge=0, le=20)
    parch: int | None = Field(None, ge=0, le=20)
    fare: float | None = Field(None, ge=0, le=10_000)
    embarked: Literal["C", "Q", "S"] | None = None
    deck: Literal["A", "B", "C", "D", "E", "F", "G"] | None = None
    who: Literal["child", "man", "woman"] | None = None
    adult_male: Binary | None = None
    alone: Binary | None = None
    clear: list[Literal["age", "fare", "embarked", "deck"]] = Field(
        default_factory=list, description="Columns to set to NULL (unknown)."
    )


class ChangeRequest(BaseModel):
    id: str
    operation: Literal["insert", "update", "delete"]
    passenger_id: int | None
    payload: dict[str, Any]
    before: dict[str, Any] | None
    reason: str
    status: Literal["pending", "applied", "rejected", "conflict", "expired"]
    requested_by: str
    requested_at: str
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_comment: str | None = None
    result: dict[str, Any] | None = None


class ChangeRequestList(BaseModel):
    change_requests: list[ChangeRequest]
    row_count: int

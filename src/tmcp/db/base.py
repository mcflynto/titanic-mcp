"""Database adapter interface (DS-05). The tools use only this interface."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class ReadDatabase(Protocol):
    dialect: str

    async def fetch(self, sql: str, params: dict[str, Any], *, max_rows: int, timeout_s: float) -> QueryResult:
        """Run one read-only statement. Return at most ``max_rows`` rows; ``truncated`` shows if more exist."""
        ...

    async def check(self) -> None:
        """Raise an exception if the database is not available (readiness probe)."""
        ...

"""Read-only SQLite adapter for the reference dataset (DS-02, QS-05)."""

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

import anyio

from tmcp.db.base import QueryResult
from tmcp.db.catalog import REFERENCE_VIEW_SQL
from tmcp.errors import AppError, ErrorCode
from tmcp.observability import metrics

_PROGRESS_STEPS = 1000


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReferenceDatabase:
    """Opens one short-lived connection for each query. The file is immutable, so this is cheap and thread-safe."""

    dialect = "sqlite"

    def __init__(self, path: Path, expected_sha256: str | None = None) -> None:
        self.path = path
        if not path.is_file():
            raise FileNotFoundError(f"Reference database not found: {path}")
        self.sha256 = file_sha256(path)
        if expected_sha256 and expected_sha256 != self.sha256:
            raise RuntimeError(f"DS-01: checksum of {path} is {self.sha256}, expected {expected_sha256}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
        conn.execute(REFERENCE_VIEW_SQL)  # temp schema only; the file does not change
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
        return conn

    def _fetch_sync(self, sql: str, params: dict[str, Any], max_rows: int, timeout_s: float) -> QueryResult:
        deadline = time.monotonic() + timeout_s
        conn = self._connect()
        try:
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_STEPS)
            cursor = conn.execute(sql, params)
            columns = [d[0] for d in cursor.description or ()]
            rows = cursor.fetchmany(max_rows + 1)
            truncated = len(rows) > max_rows
            return QueryResult(columns, [list(r) for r in rows[:max_rows]], truncated)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise AppError(
                    ErrorCode.QUERY_TIMEOUT,
                    f"The query used more than {timeout_s:g} s.",
                    hint="Add filters or aggregate the data.",
                ) from exc
            raise AppError(ErrorCode.QUERY_REJECTED, f"SQLite rejected the query: {exc}") from exc
        finally:
            conn.close()

    async def fetch(self, sql: str, params: dict[str, Any], *, max_rows: int, timeout_s: float) -> QueryResult:
        start = time.perf_counter()
        try:
            return await anyio.to_thread.run_sync(self._fetch_sync, sql, params, max_rows, timeout_s)
        finally:
            metrics.QUERY_DURATION.labels("reference").observe(time.perf_counter() - start)

    async def check(self) -> None:
        def _probe() -> None:
            conn = self._connect()
            try:
                conn.execute("SELECT count(*) FROM passengers").fetchone()
            finally:
                conn.close()

        await anyio.to_thread.run_sync(_probe)

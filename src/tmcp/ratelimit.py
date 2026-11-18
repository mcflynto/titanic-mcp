"""Rate limits for each agent (NFR-05). Version 1.0 counts in each pod (OP-04)."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from tmcp.errors import AppError, ErrorCode
from tmcp.policy.models import Limits


@dataclass
class _Bucket:
    tokens: float
    updated: float
    inflight: int = 0


class RateLimiter:
    """Token bucket (requests per minute) and a counter for parallel requests. One event loop, so no lock."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    @asynccontextmanager
    async def limit(self, agent_id: str, limits: Limits) -> AsyncIterator[None]:
        now = time.monotonic()
        capacity = float(limits.requests_per_minute)
        bucket = self._buckets.setdefault(agent_id, _Bucket(tokens=capacity, updated=now))
        bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * capacity / 60.0)
        bucket.updated = now
        if bucket.tokens < 1.0:
            retry_after = round((1.0 - bucket.tokens) * 60.0 / capacity, 1)
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Rate limit of {limits.requests_per_minute} requests per minute reached.",
                hint=f"Wait {retry_after} s.",
                details={"retry_after_s": retry_after},
            )
        if bucket.inflight >= limits.max_concurrency:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Limit of {limits.max_concurrency} parallel requests reached.",
                hint="Wait for your other calls to complete.",
                details={"retry_after_s": 1.0},
            )
        bucket.tokens -= 1.0
        bucket.inflight += 1
        try:
            yield
        finally:
            bucket.inflight -= 1

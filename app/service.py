from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel
from redis.asyncio import Redis
import time


class Algorithm(str, Enum):
    FIXED_WINDOW = "fixed-window"
    SLIDING_WINDOW = "sliding-window"
    TOKEN_BUCKET = "token-bucket"


@dataclass
class CheckParams:
    identity: str
    resource: Optional[str] = None
    algorithm: Algorithm = Algorithm.FIXED_WINDOW
    limit: Optional[int] = None
    window_ms: Optional[int] = None
    tokens_per_interval: Optional[int] = None


class RateLimitDecision(BaseModel):
    allowed: bool
    remaining: int
    limit: int
    reset_ms: int
    retry_after_ms: int
    algorithm: Algorithm
    headers: Dict[str, str]


class RateLimiterService:
    def __init__(
        self,
        redis: Redis,
        default_limit: int = 100,
        default_window_ms: int = 60_000,
    ) -> None:
        self.redis = redis
        self.default_limit = default_limit
        self.default_window_ms = default_window_ms

    async def check(self, params: CheckParams) -> RateLimitDecision:
        algorithm = params.algorithm or Algorithm.FIXED_WINDOW
        if algorithm == Algorithm.FIXED_WINDOW:
            return await self._check_fixed_window(params)
        if algorithm == Algorithm.SLIDING_WINDOW:
            return await self._check_sliding_window(params)
        if algorithm == Algorithm.TOKEN_BUCKET:
            return await self._check_token_bucket(params)
        return await self._check_fixed_window(params)

    def _key(self, params: CheckParams, suffix: str) -> str:
        base = params.identity
        if params.resource:
            base = f"{base}:{params.resource}"
        return f"rl:{base}:{suffix}"

    async def _check_fixed_window(self, params: CheckParams) -> RateLimitDecision:
        limit = params.limit or self.default_limit
        window_ms = params.window_ms or self.default_window_ms
        window_sec = int((window_ms + 999) / 1000)
        key = self._key(params, "fw")

        now = int(time.time() * 1000)
        window_start = (now // window_ms) * window_ms
        reset_ms = window_start + window_ms - now

        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_sec)
        count, _ = await pipe.execute()

        remaining = max(0, limit - int(count))
        allowed = int(count) <= limit
        retry_after_ms = 0 if allowed else reset_ms

        headers = self._build_headers(
            limit=limit,
            remaining=remaining,
            retry_after_ms=retry_after_ms,
            reset_ms=reset_ms,
            algorithm=Algorithm.FIXED_WINDOW,
        )

        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            limit=limit,
            reset_ms=reset_ms,
            retry_after_ms=retry_after_ms,
            algorithm=Algorithm.FIXED_WINDOW,
            headers=headers,
        )

    async def _check_sliding_window(self, params: CheckParams) -> RateLimitDecision:
        limit = params.limit or self.default_limit
        window_ms = params.window_ms or self.default_window_ms
        key = self._key(params, "sw")

        now = int(time.time() * 1000)
        window_start = now - window_ms

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.pexpire(key, window_ms)
        _, _, count, _ = await pipe.execute()

        remaining = max(0, limit - int(count))
        allowed = int(count) <= limit
        reset_ms = window_ms
        retry_after_ms = 0 if allowed else reset_ms

        headers = self._build_headers(
            limit=limit,
            remaining=remaining,
            retry_after_ms=retry_after_ms,
            reset_ms=reset_ms,
            algorithm=Algorithm.SLIDING_WINDOW,
        )

        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            limit=limit,
            reset_ms=reset_ms,
            retry_after_ms=retry_after_ms,
            algorithm=Algorithm.SLIDING_WINDOW,
            headers=headers,
        )

    async def _check_token_bucket(self, params: CheckParams) -> RateLimitDecision:
        capacity = params.limit or self.default_limit
        interval_ms = params.window_ms or self.default_window_ms
        refill_tokens = params.tokens_per_interval or capacity

        tokens_key = self._key(params, "tb:tokens")
        ts_key = self._key(params, "tb:ts")

        now = int(time.time() * 1000)

        pipe = self.redis.pipeline()
        pipe.mget(tokens_key, ts_key)
        ((tokens_str, ts_str),) = await pipe.execute()

        tokens = float(tokens_str) if tokens_str is not None else float(capacity)
        last_ts = int(ts_str) if ts_str is not None else now

        if now > last_ts:
            elapsed = now - last_ts
            rate_per_ms = refill_tokens / float(interval_ms)
            tokens = min(float(capacity), tokens + elapsed * rate_per_ms)

        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0

        ttl_ms = interval_ms
        pipe = self.redis.pipeline()
        pipe.set(tokens_key, str(tokens), px=ttl_ms)
        pipe.set(ts_key, str(now), px=ttl_ms)
        await pipe.execute()

        remaining = int(tokens)

        rate_per_ms = refill_tokens / float(interval_ms)
        deficit = max(0.0, 1.0 - tokens)
        retry_after_ms = 0
        if not allowed and rate_per_ms > 0:
            retry_after_ms = int(deficit / rate_per_ms)

        headers = self._build_headers(
            limit=capacity,
            remaining=remaining,
            retry_after_ms=retry_after_ms,
            reset_ms=ttl_ms,
            algorithm=Algorithm.TOKEN_BUCKET,
        )

        return RateLimitDecision(
            allowed=allowed,
            remaining=remaining,
            limit=capacity,
            reset_ms=ttl_ms,
            retry_after_ms=retry_after_ms,
            algorithm=Algorithm.TOKEN_BUCKET,
            headers=headers,
        )

    def _build_headers(
        self,
        *,
        limit: int,
        remaining: int,
        retry_after_ms: int,
        reset_ms: int,
        algorithm: Algorithm,
    ) -> Dict[str, str]:
        retry_after_sec = (
            int((retry_after_ms + 999) / 1000) if retry_after_ms > 0 else 0
        )
        reset_sec = int((reset_ms + 999) / 1000) if reset_ms > 0 else 0

        headers: Dict[str, str] = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_sec),
            "X-RateLimit-Algorithm": algorithm.value,
        }
        if retry_after_sec > 0:
            headers["Retry-After"] = str(retry_after_sec)
        return headers

"""
Redis client singleton for the rate limiter service.

Uses REDIS_URL (default redis://localhost:6379) and returns a single
async Redis connection used by all limit checks. decode_responses=True
so we work with string values.

Developed by Sydney Edwards.
"""
import os
from typing import Optional

from redis.asyncio import Redis

# Module-level singleton; created on first get_redis() call
_redis: Optional[Redis] = None


def get_redis() -> Redis:
    """Return the shared Redis client, creating it from REDIS_URL if needed."""
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis = Redis.from_url(url, decode_responses=True)
    return _redis

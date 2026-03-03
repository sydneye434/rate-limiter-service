import os
from typing import Optional

from redis.asyncio import Redis

_redis: Optional[Redis] = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis = Redis.from_url(url, decode_responses=True)
    return _redis

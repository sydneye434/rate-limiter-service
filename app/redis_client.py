import os

from redis.asyncio import Redis

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis = Redis.from_url(url, decode_responses=True)
    return _redis



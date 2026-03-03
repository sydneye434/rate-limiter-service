import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.service import Algorithm, CheckParams, RateLimiterService


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.results = []

    def incr(self, key):
        current = int(self.redis.kv.get(key, "0"))
        current += 1
        self.redis.kv[key] = str(current)
        self.results.append(current)
        return self

    def expire(self, key, seconds):
        self.redis.expiries[key] = seconds
        self.results.append(True)
        return self

    def zremrangebyscore(self, key, min_score, max_score):
        items = self.redis.zsets.get(key, [])
        self.redis.zsets[key] = [
            (score, member)
            for score, member in items
            if not (min_score <= score <= max_score)
        ]
        self.results.append(0)
        return self

    def zadd(self, key, mapping):
        items = self.redis.zsets.setdefault(key, [])
        for member, score in mapping.items():
            items.append((score, member))
        self.results.append(1)
        return self

    def zcard(self, key):
        items = self.redis.zsets.get(key, [])
        self.results.append(len(items))
        return self

    def pexpire(self, key, ms):
        self.redis.expiries[key] = ms / 1000.0
        self.results.append(True)
        return self

    def mget(self, *keys):
        vals = [self.redis.kv.get(k) for k in keys]
        self.results.append(tuple(vals))
        return self

    def set(self, key, value, px=None):
        self.redis.kv[key] = value
        self.results.append(True)
        return self

    async def execute(self):
        res = self.results
        self.results = []
        return res


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.zsets = {}
        self.expiries = {}

    def pipeline(self):
        return FakePipeline(self)


def make_service(limit=5, window_ms=1000):
    fake = FakeRedis()
    return RateLimiterService(fake, default_limit=limit, default_window_ms=window_ms)


def test_healthz_endpoint():
    client = TestClient(main.app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_fixed_window_allows_then_blocks():
    service = make_service(limit=2, window_ms=60_000)

    async def run():
        params = CheckParams(
            identity="user1", resource="/demo", algorithm=Algorithm.FIXED_WINDOW
        )
        first = await service.check(params)
        second = await service.check(params)
        third = await service.check(params)

        assert first.allowed is True
        assert second.allowed is True
        assert third.allowed is False
        assert first.headers["X-RateLimit-Limit"] == "2"

    asyncio.run(run())


def test_sliding_window_counts_requests():
    service = make_service(limit=2, window_ms=60_000)

    async def run():
        params = CheckParams(
            identity="user2", resource="/demo", algorithm=Algorithm.SLIDING_WINDOW
        )
        first = await service.check(params)
        second = await service.check(params)
        third = await service.check(params)

        assert first.allowed is True
        assert second.allowed is True
        assert third.allowed is False

    asyncio.run(run())


def test_token_bucket_refills_and_limits():
    service = make_service(limit=2, window_ms=60_000)

    async def run():
        params = CheckParams(
            identity="user3",
            resource="/demo",
            algorithm=Algorithm.TOKEN_BUCKET,
            tokens_per_interval=2,
        )
        first = await service.check(params)
        second = await service.check(params)
        third = await service.check(params)

        assert first.allowed is True
        assert second.allowed is True
        assert third.allowed is False

    asyncio.run(run())


def test_check_limit_post_with_fake_service():
    """Covers get_service, resolve_identity (X-API-Key), check_limit."""
    service = make_service(limit=10, window_ms=60_000)
    main.app.dependency_overrides[main.get_service] = lambda: service
    try:
        client = TestClient(main.app)
        resp = client.post(
            "/check-limit",
            json={"algorithm": "fixed-window", "limit": 5},
            headers={"X-API-Key": "key1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] is True
        assert "X-RateLimit-Limit" in resp.headers
    finally:
        main.app.dependency_overrides.clear()


def test_check_limit_uses_x_user_id():
    """Covers resolve_identity X-User-Id path."""
    service = make_service(limit=10, window_ms=60_000)
    main.app.dependency_overrides[main.get_service] = lambda: service
    try:
        client = TestClient(main.app)
        resp = client.post(
            "/check-limit",
            json={"algorithm": "sliding-window"},
            headers={"X-User-Id": "user-42"},
        )
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_check_limit_invalid_algorithm():
    """Covers 400 on invalid algorithm."""
    service = make_service(limit=10, window_ms=60_000)
    main.app.dependency_overrides[main.get_service] = lambda: service
    try:
        client = TestClient(main.app)
        resp = client.post("/check-limit", json={"algorithm": "invalid"})
        assert resp.status_code == 400
    finally:
        main.app.dependency_overrides.clear()


def test_demo_fixed_with_fake_service():
    """Covers rate_limit_dependency and middleware headers."""
    service = make_service(limit=10, window_ms=60_000)
    main.app.dependency_overrides[main.get_service] = lambda: service
    try:
        client = TestClient(main.app)
        resp = client.get("/demo/fixed", headers={"X-API-Key": "demo-key"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "algorithm": "fixed-window"}
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        resp2 = client.get("/demo/fixed", headers={"X-API-Key": "demo-key"})
        assert resp2.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_get_redis_creates_client_once():
    """Covers redis_client get_redis when _redis is None."""
    import app.redis_client as rc

    orig = rc._redis
    try:
        rc._redis = None
        with patch("app.redis_client.Redis") as mock_redis:
            mock_redis.from_url.return_value = "fake_conn"
            out = rc.get_redis()
            assert out == "fake_conn"
            mock_redis.from_url.assert_called_once()
    finally:
        rc._redis = orig


def test_token_bucket_refill_branch():
    """Covers token bucket refill when now > last_ts."""
    fake = FakeRedis()

    async def run():
        with patch("app.service.time") as mock_time:
            mock_time.time.return_value = 0
            params = CheckParams(
                identity="refill-user",
                resource="/r",
                algorithm=Algorithm.TOKEN_BUCKET,
                limit=2,
                window_ms=60_000,
            )
            svc = RateLimiterService(fake, default_limit=2, default_window_ms=60_000)
            await svc.check(params)
            mock_time.time.return_value = 50_000
            decision = await svc.check(params)
            assert decision.allowed is True
            assert decision.remaining >= 0

    asyncio.run(run())

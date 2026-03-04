# Rate Limiter Service

A production-style rate limiting service built with **FastAPI** and **Redis**. It supports multiple algorithms (fixed window, sliding window, token bucket), runs across distributed app instances via a shared Redis backend, and exposes a **Rate Limit as a Service (RLaaS)** API that any backend can call.

## Features

- **Algorithms**: Fixed window, sliding window, token bucket
- **Identity**: Rate limits keyed by API key (`X-API-Key`), user ID (`X-User-Id`), or client IP
- **HTTP headers**: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After` when limited
- **RLaaS**: `POST /check-limit` returns allow/deny and metadata for integration from other apps
- **Demo routes**: `/demo/fixed`, `/demo/sliding`, `/demo/token-bucket` with built-in rate limiting

## Prerequisites

- **Python 3.9+** (3.12 recommended)
- **Redis** (local or remote) for the limiter state
- **Docker** and **Docker Compose** (optional, for containerized runs)

## Quick start

### 1. Clone and enter the repo

```bash
git clone <repo-url>
cd rate-limiter-service
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Start Redis

**Option A – Docker**

```bash
docker run -d -p 6379:6379 redis:7-alpine
```

**Option B – Local Redis**

Ensure Redis is running on `localhost:6379` (or set `REDIS_URL`).

### 4. Run the service

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- API: **http://localhost:8000**
- Docs: **http://localhost:8000/docs**

## Configuration

| Variable     | Default               | Description                    |
|-------------|------------------------|--------------------------------|
| `PORT`      | `8000`                | Port the app listens on        |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL        |

Create a `.env` in the project root or export these before starting the app.

## API usage

### Health check

```bash
curl http://localhost:8000/healthz
```

### RLaaS: check rate limit

**POST** `/check-limit` – call from your app to decide if a request is allowed and get headers.

**Request body (JSON):**

| Field               | Type   | Description                                      |
|---------------------|--------|--------------------------------------------------|
| `key`               | string | Optional; overrides identity from headers        |
| `algorithm`         | string | `fixed-window`, `sliding-window`, or `token-bucket` |
| `resource`          | string | Optional scope (e.g. endpoint path)               |
| `limit`             | number | Max requests (or bucket capacity for token-bucket)|
| `windowMs`         | number | Window or refill interval in milliseconds         |
| `tokensPerInterval` | number | Optional; refill rate for token-bucket            |

**Identity** is taken from (first present):

1. Body `key`
2. Header `X-API-Key`
3. Header `X-User-Id`
4. Client IP

**Example – fixed window (100/min):**

```bash
curl -X POST http://localhost:8000/check-limit \
  -H "Content-Type: application/json" \
  -H "X-API-Key: my-key" \
  -d '{"algorithm": "fixed-window", "limit": 100, "windowMs": 60000}'
```

**Example – token bucket:**

```bash
curl -X POST http://localhost:8000/check-limit \
  -H "Content-Type: application/json" \
  -H "X-User-Id: user-123" \
  -d '{"algorithm": "token-bucket", "limit": 50, "windowMs": 60000, "tokensPerInterval": 50}'
```

**Response (200 allowed / 429 limited):**

- Body: `allowed`, `remaining`, `limit`, `reset_ms`, `retry_after_ms`, `algorithm`, `headers`
- Response headers include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `Retry-After` when limited.

### Demo endpoints

These routes use the same limiter with different algorithms; identity comes from headers or IP.

```bash
curl -H "X-API-Key: demo" http://localhost:8000/demo/fixed
curl -H "X-API-Key: demo" http://localhost:8000/demo/sliding
curl -H "X-API-Key: demo" http://localhost:8000/demo/token-bucket
```

## Running with Docker

### Build and run the app + Redis

```bash
docker compose up --build
```

- Redis: `localhost:6379`
- App instance 1: **http://localhost:8000**
- App instance 2: **http://localhost:8001**

Both app containers share the same Redis, so rate limits are consistent across instances.

### Run only the app image

```bash
docker build -t rate-limiter-service .
docker run -p 8000:8000 -e REDIS_URL=redis://host.docker.internal:6379 rate-limiter-service
```

## Development

### Virtual environment (venv)

Use a venv named `venv` (or activate your preferred env):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install black pytest pytest-cov
```

### Format and lint

```bash
black app tests
black --check app tests
```

Or, if your Makefile has these targets:

```bash
make format
make lint
```

### Run tests and coverage

```bash
pytest --cov=app --cov-report=term-missing --cov-fail-under=85
```

With venv:

```bash
venv/bin/pytest --cov=app --cov-report=term-missing --cov-fail-under=85
```

## Scaling

### Horizontal scaling (app tier)

You can run **multiple instances** of the rate limiter service behind a load balancer. Every instance must use the **same Redis** (`REDIS_URL`). Limits are stored in Redis (counters, sorted sets, keys), so all instances see the same state and enforce a single, consistent limit per identity and resource.

- **Add more app replicas**: Scale the app only (e.g. more containers or processes). No code changes; set the same `REDIS_URL` on each instance.
- **Load balancing**: Put a reverse proxy (e.g. nginx, cloud LB) in front of the app instances. Client identity is taken from headers or IP, so the LB should forward `X-API-Key` / `X-User-Id` and preserve client IP if you rate limit by IP.

The `docker-compose` setup runs two app containers (ports 8000 and 8001) against one Redis; you can add more app services or increase replicas in an orchestrator.

### Redis as the single source of truth

Rate limit state lives only in Redis. That gives:

- **Consistency** across all app instances (no per-instance drift).
- **Atomicity** via Redis commands (`INCR`, `EXPIRE`, `ZADD`, etc.), so concurrent requests are counted correctly.

If Redis is down or unreachable, the service cannot apply limits reliably; consider handling Redis errors in your client (e.g. fail open or fail closed depending on policy).

### Redis capacity and high availability

- **Throughput**: Each check does a small number of Redis operations. A single Redis node can typically handle a large number of limit checks per second. If you outgrow one node, consider **Redis Cluster** (with a compatible client and key layout) or splitting limits across multiple Redis instances by identity/resource.
- **Memory**: Usage grows with the number of distinct identities and resources (keys and, for sliding window, sorted-set entries). Set Redis `maxmemory` and an eviction policy if needed.
- **Availability**: For production, run Redis in a **high-availability** setup (e.g. Redis Sentinel or managed Redis with automatic failover) and point `REDIS_URL` at the endpoint that handles failover.

### Summary

| Goal | Approach |
|------|----------|
| More request throughput | Add app replicas; keep one shared Redis (or Redis Cluster if required). |
| Consistent limits across instances | Use a single Redis (or Redis Cluster) for all app instances. |
| Redis HA | Use Redis Sentinel or a managed Redis service; configure `REDIS_URL` accordingly. |
| Very large key space | Consider `maxmemory` and eviction; or shard by identity/resource across Redis instances (would require code changes). |

## Project layout

```
rate-limiter-service/
├── app/
│   ├── main.py          # FastAPI app, routes, middleware
│   ├── redis_client.py  # Redis connection
│   └── service.py      # Rate limit algorithms and logic
├── tests/
│   └── test_smoke.py   # Unit and API tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml      # Black config
└── README.md
```

## License

MIT.

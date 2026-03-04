"""
Rate Limiter Service – FastAPI application entrypoint.

Exposes health check, RLaaS /check-limit endpoint, and demo routes with
rate limiting. Identity is resolved from X-API-Key, X-User-Id, or client IP.

Developed by Sydney Edwards.
"""
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import os
import time
from typing import Optional

from .redis_client import get_redis
from .service import (
    Algorithm,
    CheckParams,
    RateLimiterService,
)

app = FastAPI(title="Rate Limiter Service", version="1.0.0")


def get_service() -> RateLimiterService:
    """Build and return the rate limiter service using the shared Redis client."""
    redis = get_redis()
    return RateLimiterService(redis)


def resolve_identity(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> str:
    """Resolve rate-limit identity: API key > user ID > client IP."""
    if x_api_key:
        return x_api_key
    if x_user_id:
        return x_user_id
    return request.client.host if request.client else "unknown"


@app.get("/healthz")
async def healthz():
    """Liveness/readiness probe; returns status and current time."""
    return {"status": "ok", "time": int(time.time())}


@app.post("/check-limit")
async def check_limit(
    payload: dict,
    request: Request,
    service: RateLimiterService = Depends(get_service),
    identity: str = Depends(resolve_identity),
):
    """
    RLaaS endpoint: check if the request is within rate limit.

    Body may include algorithm, limit, windowMs, tokensPerInterval, resource, key.
    Returns 200 with decision + headers if allowed, 429 if limited.
    """
    algo_str = payload.get("algorithm") or "fixed-window"
    try:
        algorithm = Algorithm(algo_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid algorithm") from None

    limit = payload.get("limit")
    window_ms = payload.get("windowMs")
    tokens_per_interval = payload.get("tokensPerInterval")
    resource = payload.get("resource") or request.url.path

    params = CheckParams(
        identity=identity,
        resource=resource,
        algorithm=algorithm,
        limit=limit,
        window_ms=window_ms,
        tokens_per_interval=tokens_per_interval,
    )

    decision = await service.check(params)
    headers = decision.headers
    status = 200 if decision.allowed else 429

    return JSONResponse(
        status_code=status, content=decision.model_dump(), headers=headers
    )


def rate_limit_dependency(
    algorithm: Algorithm,
    limit: int,
    window_ms: int,
):
    """
    FastAPI dependency that runs the rate limiter and raises 429 if over limit.
    Attaches rate limit headers to request.state for the middleware to add to the response.
    """

    async def dependency(
        request: Request,
        service: RateLimiterService = Depends(get_service),
        identity: str = Depends(resolve_identity),
    ):
        params = CheckParams(
            identity=identity,
            resource=request.url.path,
            algorithm=algorithm,
            limit=limit,
            window_ms=window_ms,
        )
        decision = await service.check(params)
        if not decision.allowed:
            raise HTTPException(status_code=429, detail=decision.model_dump())
        for k, v in decision.headers.items():
            request.state.response_headers = getattr(
                request.state, "response_headers", {}
            )
            request.state.response_headers[k] = v

        return decision

    return dependency


@app.middleware("http")
async def apply_rate_limit_headers(request: Request, call_next):
    """Copy rate limit headers from request.state onto the response (set by rate_limit_dependency)."""
    response = await call_next(request)
    extra_headers = getattr(request.state, "response_headers", {})
    for k, v in extra_headers.items():
        response.headers[k] = v
    return response


# Demo routes: each uses a different algorithm with fixed limit/window
@app.get("/demo/fixed")
async def demo_fixed(
    _=Depends(
        rate_limit_dependency(Algorithm.FIXED_WINDOW, limit=100, window_ms=60_000)
    ),
):
    return {"ok": True, "algorithm": "fixed-window"}


@app.get("/demo/sliding")
async def demo_sliding(
    _=Depends(
        rate_limit_dependency(Algorithm.SLIDING_WINDOW, limit=100, window_ms=60_000)
    ),
):
    return {"ok": True, "algorithm": "sliding-window"}


@app.get("/demo/token-bucket")
async def demo_token_bucket(
    _=Depends(
        rate_limit_dependency(
            Algorithm.TOKEN_BUCKET,
            limit=50,
            window_ms=60_000,
        )
    ),
):
    return {"ok": True, "algorithm": "token-bucket"}

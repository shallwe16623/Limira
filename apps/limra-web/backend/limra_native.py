from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from limra_backend.routers import limra

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_client = _redis_client_from_env()
    if redis_client is not None:
        app.state.redis = redis_client
    try:
        yield
    finally:
        if redis_client is not None:
            close = getattr(redis_client, "aclose", None) or getattr(redis_client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


def _cors_origins_from_env() -> list[str]:
    raw_value = str(os.getenv("LIMRA_CORS_ALLOW_ORIGINS") or "").strip()
    if raw_value:
        return [origin.strip() for origin in raw_value.split(",") if origin.strip()]
    return ["http://127.0.0.1:5173", "http://localhost:5173"]


def _redis_client_from_env() -> Any | None:
    redis_url = str(os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        return None
    try:
        import redis.asyncio as redis
    except Exception:
        log.warning("Redis URL configured but redis package is unavailable")
        return None
    return redis.from_url(redis_url, decode_responses=True)


app = FastAPI(title="Limra API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_from_env(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type", "accept"],
)
app.include_router(limra.router, prefix="/api/limra")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"status": True}

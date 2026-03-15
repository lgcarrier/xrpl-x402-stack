from __future__ import annotations

from typing import Any


def create_async_redis_client(url: str) -> Any:
    try:
        import redis.asyncio as redis
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "redis dependency not installed; install requirements.txt to use redis_gateways mode"
        ) from exc

    return redis.from_url(url, decode_responses=True)

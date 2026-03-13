from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
from typing import Any, Protocol

from app.config import Settings
from app.redis_utils import create_async_redis_client

DEFAULT_SINGLE_GATEWAY_ID = "default-gateway"
ACTIVE_GATEWAY_STATUS = "active"


class GatewayAuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class AuthenticatedGateway:
    gateway_id: str


class GatewayAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedGateway:
        ...


def hash_gateway_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SingleTokenGatewayAuthenticator:
    def __init__(self, token: str, gateway_id: str = DEFAULT_SINGLE_GATEWAY_ID) -> None:
        self._token = token
        self._gateway_id = gateway_id

    async def authenticate(self, bearer_token: str) -> AuthenticatedGateway:
        if not secrets.compare_digest(bearer_token, self._token):
            raise GatewayAuthenticationError("invalid token")
        return AuthenticatedGateway(gateway_id=self._gateway_id)


class RedisGatewayAuthenticator:
    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def authenticate(self, bearer_token: str) -> AuthenticatedGateway:
        token_hash = hash_gateway_token(bearer_token)
        token_record = await self._redis.hgetall(f"facilitator:gateway_token:{token_hash}")
        if not token_record:
            raise GatewayAuthenticationError("unknown token")

        status = str(token_record.get("status", "")).strip().lower()
        gateway_id = str(token_record.get("gateway_id", "")).strip()
        if status != ACTIVE_GATEWAY_STATUS or not gateway_id:
            raise GatewayAuthenticationError("inactive token")

        return AuthenticatedGateway(gateway_id=gateway_id)


def build_gateway_authenticator(
    settings: Settings,
    redis_client: Any | None = None,
) -> GatewayAuthenticator:
    if settings.GATEWAY_AUTH_MODE == "single_token":
        return SingleTokenGatewayAuthenticator(
            settings.FACILITATOR_BEARER_TOKEN.get_secret_value()
        )

    if redis_client is None:
        redis_client = create_async_redis_client(settings.REDIS_URL.get_secret_value())
    return RedisGatewayAuthenticator(redis_client)

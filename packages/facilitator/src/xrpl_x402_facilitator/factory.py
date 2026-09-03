from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import logging
import secrets
from typing import Any, Final

import structlog
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from x402 import x402FacilitatorSync
from x402.extensions.bazaar import BAZAAR
from x402.interfaces import FacilitatorExtension

from xrpl_x402_core import (
    SettleRequest,
    SettleResponse,
    VerifyRequest,
    VerifyResponse,
)
from xrpl_x402_facilitator.catalog import BazaarCatalog
from xrpl_x402_facilitator.config import Settings, get_settings
from xrpl_x402_facilitator.gateway_auth import (
    AuthenticatedGateway,
    GatewayAuthenticationError,
    GatewayAuthenticator,
    build_gateway_authenticator,
)
from xrpl_x402_facilitator.idempotency import PaymentIdentifierStore
from xrpl_x402_facilitator.redis_utils import create_async_redis_client
from xrpl_x402_facilitator.xrpl_service import (
    ExactXRPLFacilitatorScheme,
    XRPLService,
)

PAYMENT_ENDPOINT_PATHS: Final[frozenset[str]] = frozenset(
    {"/verify", "/settle"}
)
AUTHENTICATION_ERROR_DETAIL: Final[str] = (
    "Invalid authentication credentials"
)
AUTHENTICATED_GATEWAY_STATE_KEY: Final[str] = "authenticated_gateway"
GATEWAY_AUTH_FAILED_STATE_KEY: Final[str] = "gateway_auth_failed"
RATE_LIMIT_STORAGE_KEY_PREFIX: Final[str] = "facilitator:ratelimit"


class PayloadTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] not in PAYMENT_ENDPOINT_PATHS
        ):
            await self.app(scope, receive, send)
            return
        content_length = Headers(scope=scope).get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > self.max_body_bytes
        ):
            await self._send_413(scope, receive, send)
            return
        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise PayloadTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except PayloadTooLargeError:
            await self._send_413(scope, receive, send)

    @staticmethod
    async def _send_413(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        await JSONResponse(
            status_code=413, content={"detail": "Request body too large"}
        )(scope, receive, send)


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=logging.INFO, format="%(message)s")


configure_logging()
logger = structlog.get_logger()


def build_rate_limiter(settings: Settings) -> Limiter:
    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri=settings.REDIS_URL.get_secret_value(),
        key_prefix=RATE_LIMIT_STORAGE_KEY_PREFIX,
    )
    storage = getattr(limiter, "_storage", None)
    if storage is None or not storage.check():
        raise RuntimeError("Redis-backed rate limiter storage is unavailable")
    return limiter


def create_app(
    app_settings: Settings | None = None,
    xrpl_service: XRPLService | None = None,
    gateway_authenticator: GatewayAuthenticator | None = None,
) -> FastAPI:
    settings = app_settings or get_settings()
    async_redis = create_async_redis_client(
        settings.REDIS_URL.get_secret_value()
    )
    mechanism = xrpl_service or ExactXRPLFacilitatorScheme(settings)
    facilitator = x402FacilitatorSync().register(
        [settings.NETWORK_ID], mechanism
    )
    facilitator.register_extension(
        FacilitatorExtension(key="payment-identifier")
    )
    if settings.ENABLE_BAZAAR:
        facilitator.register_extension(BAZAAR)
    gateway_auth = gateway_authenticator or build_gateway_authenticator(
        settings, redis_client=async_redis
    )
    limiter = build_rate_limiter(settings)
    identifiers = PaymentIdentifierStore(
        async_redis,
        ttl_seconds=max(settings.REPLAY_PROCESSED_TTL_SECONDS, 300),
    )
    catalog = BazaarCatalog(
        async_redis,
        retention_seconds=settings.DISCOVERY_RETENTION_SECONDS,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await async_redis.aclose()

    app = FastAPI(
        title="XRPL x402 Facilitator",
        description=(
            "Non-custodial x402 v2 facilitator for exact XRPL payments."
        ),
        version="0.2.0",
        docs_url="/docs" if settings.ENABLE_API_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_API_DOCS else None,
        openapi_url=(
            "/openapi.json" if settings.ENABLE_API_DOCS else None
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.MAX_REQUEST_BODY_BYTES,
    )
    app.state.settings = settings
    app.state.xrpl = mechanism
    app.state.facilitator = facilitator
    app.state.limiter = limiter
    app.state.gateway_auth = gateway_auth
    app.state.catalog = catalog
    app.add_exception_handler(
        RateLimitExceeded, _rate_limit_exceeded_handler
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        structlog.contextvars.clear_contextvars()
        try:
            if (
                request.method == "POST"
                and request.url.path in PAYMENT_ENDPOINT_PATHS
            ):
                authorization = request.headers.get("authorization")
                scheme, _, token = (
                    authorization.partition(" ")
                    if authorization
                    else ("", "", "")
                )
                if not token or not secrets.compare_digest(
                    scheme.lower(), "bearer"
                ):
                    setattr(
                        request.state,
                        GATEWAY_AUTH_FAILED_STATE_KEY,
                        True,
                    )
                else:
                    try:
                        gateway = await gateway_auth.authenticate(token.strip())
                    except GatewayAuthenticationError as exc:
                        logger.warning(
                            "payment_auth_failed", error=str(exc)
                        )
                        setattr(
                            request.state,
                            GATEWAY_AUTH_FAILED_STATE_KEY,
                            True,
                        )
                    else:
                        setattr(
                            request.state,
                            AUTHENTICATED_GATEWAY_STATE_KEY,
                            gateway,
                        )
                        request.state.gateway_id = gateway.gateway_id
                        structlog.contextvars.bind_contextvars(
                            gateway_id=gateway.gateway_id
                        )
            return await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

    def require_gateway(request: Request) -> AuthenticatedGateway:
        gateway = getattr(
            request.state, AUTHENTICATED_GATEWAY_STATE_KEY, None
        )
        if gateway is None or getattr(
            request.state, GATEWAY_AUTH_FAILED_STATE_KEY, False
        ):
            raise HTTPException(
                status_code=401,
                detail=AUTHENTICATION_ERROR_DETAIL,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return gateway

    def payment_rate_limit_key(request: Request) -> str:
        gateway_id = getattr(request.state, "gateway_id", None)
        return (
            f"gateway:{gateway_id}"
            if gateway_id
            else f"ip:{get_remote_address(request)}"
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "healthy",
            "network": settings.NETWORK_ID,
            "x402Version": "2",
        }

    @app.get("/supported")
    async def supported() -> dict[str, Any]:
        return facilitator.get_supported().model_dump(
            by_alias=True, exclude_none=True
        )

    @app.post("/verify", response_model=VerifyResponse)
    @limiter.limit("30/minute", key_func=payment_rate_limit_key)
    async def verify(
        request: Request, raw_body: dict[str, Any]
    ) -> VerifyResponse:
        require_gateway(request)
        body = _parse_request(raw_body, VerifyRequest)
        # A payment identifier binds retries to one immutable request, but a
        # successful verification is deliberately never cached. XRPL account
        # sequence, RegularKey, ticket, balance, and ledger-expiry state can
        # change between requests, so every authorization attempt must be
        # checked against current ledger state.
        await identifiers.bind(
            body.payment_payload, body.payment_requirements
        )
        result = await asyncio.to_thread(
            facilitator.verify,
            body.payment_payload,
            body.payment_requirements,
        )
        return result

    @app.post("/settle", response_model=SettleResponse)
    @limiter.limit("20/minute", key_func=payment_rate_limit_key)
    async def settle(
        request: Request,
        response: Response,
        raw_body: dict[str, Any],
    ) -> SettleResponse:
        require_gateway(request)
        body = _parse_request(raw_body, SettleRequest)
        cache_key, cached = await identifiers.get_cached(
            "settle", body.payment_payload, body.payment_requirements
        )
        if (
            cached is not None
            and cached.get("errorReason") != "settlement_pending"
        ):
            return SettleResponse.model_validate(cached)
        result = await asyncio.to_thread(
            facilitator.settle,
            body.payment_payload,
            body.payment_requirements,
        )
        if result.success and settings.ENABLE_BAZAAR:
            cataloged = await catalog.index(
                body.payment_payload, body.payment_requirements
            )
            if cataloged:
                extension_result = {"bazaar": {"status": "success"}}
                result = result.model_copy(
                    update={"extensions": extension_result}
                )
                response.headers["EXTENSION-RESPONSES"] = (
                    _encode_extension_response(extension_result)
                )
        serialized = result.model_dump(
            by_alias=True, exclude_none=True
        )
        if result.error_reason != "settlement_pending":
            await identifiers.put(cache_key, "settle", serialized)
        return result

    @app.get("/discovery/resources")
    async def discovery_resources(
        type: str | None = None,
        pay_to: str | None = Query(default=None, alias="payTo"),
        scheme: str | None = None,
        network: str | None = None,
        extensions: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        if not settings.ENABLE_BAZAAR:
            raise HTTPException(
                status_code=404,
                detail="Bazaar discovery is disabled",
            )
        return await catalog.list(
            resource_type=type,
            pay_to=pay_to,
            scheme=scheme,
            network=network,
            extension=extensions,
            limit=limit,
            offset=offset,
        )

    @app.get("/discovery/search")
    async def discovery_search(
        query: str = Query(min_length=1),
        type: str | None = None,
        pay_to: str | None = Query(default=None, alias="payTo"),
        scheme: str | None = None,
        network: str | None = None,
        extensions: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not settings.ENABLE_BAZAAR:
            raise HTTPException(
                status_code=404,
                detail="Bazaar discovery is disabled",
            )
        offset = _decode_cursor(cursor)
        result = await catalog.list(
            resource_type=type,
            pay_to=pay_to,
            scheme=scheme,
            network=network,
            extension=extensions,
            limit=limit,
            offset=offset,
            query=query,
        )
        items = result.pop("items")
        total = result["pagination"]["total"]
        next_offset = offset + len(items)
        return {
            "x402Version": 2,
            "resources": items,
            "partialResults": next_offset < total,
            "pagination": {
                "limit": limit,
                "cursor": (
                    _encode_cursor(next_offset)
                    if next_offset < total
                    else None
                ),
            },
        }

    return app


def _parse_request(
    raw: dict[str, Any],
    model_type: type[VerifyRequest] | type[SettleRequest],
):
    _validate_canonical_wire_fields(raw)
    try:
        body = model_type.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400, detail=exc.errors(include_url=False)
        ) from exc
    if body.x402_version != 2 or body.payment_payload.x402_version != 2:
        raise HTTPException(
            status_code=400, detail="Only x402Version 2 is supported"
        )
    return body


_FACILITATOR_FIELDS = frozenset(
    {"x402Version", "paymentPayload", "paymentRequirements"}
)
_PAYMENT_PAYLOAD_FIELDS = frozenset(
    {"x402Version", "payload", "accepted", "resource", "extensions"}
)
_PAYMENT_REQUIREMENTS_FIELDS = frozenset(
    {
        "scheme",
        "network",
        "asset",
        "amount",
        "payTo",
        "maxTimeoutSeconds",
        "extra",
    }
)
_XRPL_PAYLOAD_FIELDS = frozenset({"signedTxBlob"})
_XRPL_EXTRA_FIELDS = frozenset(
    {
        "issuer",
        "areFeesSponsored",
        "assetTransferMethod",
        "invoiceId",
        "destinationTag",
    }
)
_RESOURCE_FIELDS = frozenset(
    {"url", "description", "mimeType", "serviceName", "tags", "iconUrl"}
)


def _validate_canonical_wire_fields(raw: dict[str, Any]) -> None:
    """Reject aliases and unknown fields ignored by permissive upstream models."""

    _reject_unexpected_fields(raw, _FACILITATOR_FIELDS, "facilitator request")
    payload = raw.get("paymentPayload")
    _reject_unexpected_fields(
        payload, _PAYMENT_PAYLOAD_FIELDS, "paymentPayload"
    )
    if isinstance(payload, dict):
        _reject_unexpected_fields(
            payload.get("payload"),
            _XRPL_PAYLOAD_FIELDS,
            "paymentPayload.payload",
        )
        _validate_requirements_fields(
            payload.get("accepted"), "paymentPayload.accepted"
        )
        _reject_unexpected_fields(
            payload.get("resource"),
            _RESOURCE_FIELDS,
            "paymentPayload.resource",
        )
    _validate_requirements_fields(
        raw.get("paymentRequirements"), "paymentRequirements"
    )


def _validate_requirements_fields(value: Any, path: str) -> None:
    _reject_unexpected_fields(value, _PAYMENT_REQUIREMENTS_FIELDS, path)
    if isinstance(value, dict):
        _reject_unexpected_fields(
            value.get("extra"), _XRPL_EXTRA_FIELDS, f"{path}.extra"
        )


def _reject_unexpected_fields(
    value: Any, allowed: frozenset[str], path: str
) -> None:
    if not isinstance(value, dict):
        return
    unexpected = set(value) - allowed
    if unexpected:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unexpected fields at {path}: "
                + ", ".join(sorted(unexpected))
            ),
        )


def _encode_extension_response(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode()
    return base64.b64encode(raw).decode("ascii")


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(
            0,
            int(base64.urlsafe_b64decode(cursor.encode()).decode()),
        )
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400, detail="Invalid discovery cursor"
        ) from None

from contextlib import asynccontextmanager
import logging
import secrets
from typing import Final

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xrpl_x402_facilitator.config import Settings, get_settings
from xrpl_x402_facilitator.gateway_auth import (
    AuthenticatedGateway,
    GatewayAuthenticationError,
    GatewayAuthenticator,
    build_gateway_authenticator,
)
from xrpl_x402_facilitator.models import PaymentRequest, SettleResponse, SupportedResponse, VerifyResponse
from xrpl_x402_facilitator.redis_utils import create_async_redis_client
from xrpl_x402_facilitator.xrpl_service import XRPLService

PAYMENT_ENDPOINT_PATHS: Final[frozenset[str]] = frozenset({"/verify", "/settle"})
AUTHENTICATION_ERROR_DETAIL: Final[str] = "Invalid authentication credentials"
AUTHENTICATED_GATEWAY_STATE_KEY: Final[str] = "authenticated_gateway"
GATEWAY_AUTH_FAILED_STATE_KEY: Final[str] = "gateway_auth_failed"
RATE_LIMIT_STORAGE_KEY_PREFIX: Final[str] = "facilitator:ratelimit"


class PayloadTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] not in PAYMENT_ENDPOINT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await self._send_413(scope, receive, send)
                    return
            except ValueError:
                pass

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

    async def _send_413(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
        await response(scope, receive, send)


def configure_logging() -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            timestamper,
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
    limiter_kwargs: dict[str, object] = {
        "key_func": get_remote_address,
        "storage_uri": settings.REDIS_URL.get_secret_value(),
        "key_prefix": RATE_LIMIT_STORAGE_KEY_PREFIX,
    }

    try:
        limiter = Limiter(**limiter_kwargs)
    except Exception as exc:
        raise RuntimeError("Unable to initialize Redis-backed rate limiter") from exc

    storage = getattr(limiter, "_storage", None)
    if storage is None:
        raise RuntimeError("Redis-backed rate limiter did not expose a storage backend")

    try:
        if not storage.check():
            raise RuntimeError("Redis-backed rate limiter storage is unavailable")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Redis-backed rate limiter storage is unavailable") from exc

    return limiter


def create_app(
    app_settings: Settings | None = None,
    xrpl_service: XRPLService | None = None,
    gateway_authenticator: GatewayAuthenticator | None = None,
) -> FastAPI:
    active_settings = app_settings or get_settings()
    redis_client = None
    if xrpl_service is None or (
        gateway_authenticator is None and active_settings.gateway_auth_uses_redis()
    ):
        redis_client = create_async_redis_client(active_settings.REDIS_URL.get_secret_value())

    active_xrpl_service = xrpl_service or XRPLService(active_settings, redis_client=redis_client)
    active_gateway_auth = gateway_authenticator or build_gateway_authenticator(
        active_settings,
        redis_client=redis_client,
    )
    limiter = build_rate_limiter(active_settings)

    def _payment_rate_limit_key(request: Request) -> str:
        gateway_id = getattr(request.state, "gateway_id", None)
        if isinstance(gateway_id, str) and gateway_id:
            return f"gateway:{gateway_id}"
        return f"ip:{get_remote_address(request)}"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if redis_client is not None:
                await redis_client.aclose()

    app = FastAPI(
        title="XRPL x402 Facilitator",
        description=(
            "Self-hosted x402 facilitator for verifying and settling presigned XRPL "
            "payment transactions."
        ),
        version="0.1.0",
        docs_url="/docs" if active_settings.ENABLE_API_DOCS else None,
        redoc_url="/redoc" if active_settings.ENABLE_API_DOCS else None,
        openapi_url="/openapi.json" if active_settings.ENABLE_API_DOCS else None,
        lifespan=lifespan,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=active_settings.MAX_REQUEST_BODY_BYTES,
    )
    app.state.settings = active_settings
    app.state.xrpl = active_xrpl_service
    app.state.limiter = limiter
    app.state.gateway_auth = active_gateway_auth
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        structlog.contextvars.clear_contextvars()
        try:
            if request.method == "POST" and request.url.path in PAYMENT_ENDPOINT_PATHS:
                authorization = request.headers.get("authorization")
                scheme, _, token = (
                    authorization.partition(" ") if authorization else ("", "", "")
                )
                if (
                    not authorization
                    or not secrets.compare_digest(scheme.lower(), "bearer")
                    or not token
                ):
                    setattr(request.state, GATEWAY_AUTH_FAILED_STATE_KEY, True)
                else:
                    try:
                        gateway = await active_gateway_auth.authenticate(token.strip())
                    except GatewayAuthenticationError as exc:
                        logger.warning("payment_auth_failed", error=str(exc))
                        setattr(request.state, GATEWAY_AUTH_FAILED_STATE_KEY, True)
                    else:
                        setattr(request.state, AUTHENTICATED_GATEWAY_STATE_KEY, gateway)
                        request.state.gateway_id = gateway.gateway_id
                        structlog.contextvars.bind_contextvars(gateway_id=gateway.gateway_id)
            return await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()

    def _unauthorized() -> HTTPException:
        return HTTPException(
            status_code=401,
            detail=AUTHENTICATION_ERROR_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _require_authenticated_gateway(request: Request) -> AuthenticatedGateway:
        gateway = getattr(request.state, AUTHENTICATED_GATEWAY_STATE_KEY, None)
        if gateway is None or getattr(request.state, GATEWAY_AUTH_FAILED_STATE_KEY, False):
            raise _unauthorized()
        return gateway

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy", "network": active_settings.NETWORK_ID}

    @app.get("/supported", response_model=SupportedResponse)
    async def supported() -> SupportedResponse:
        return SupportedResponse(
            network=active_settings.NETWORK_ID,
            assets=active_xrpl_service.supported_assets(),
            settlement_mode=active_settings.SETTLEMENT_MODE,
        )

    @app.post("/verify", response_model=VerifyResponse)
    @limiter.limit("30/minute", key_func=_payment_rate_limit_key)
    async def verify(
        request: Request,
        body: PaymentRequest,
    ) -> VerifyResponse:
        _require_authenticated_gateway(request)
        if not body.signed_tx_blob:
            raise HTTPException(status_code=400, detail="signed_tx_blob required")
        try:
            return await active_xrpl_service.verify_payment(body.signed_tx_blob, body.invoice_id)
        except ValueError as exc:
            logger.warning("verify_failed", error=str(exc))
            raise HTTPException(status_code=402, detail=str(exc)) from exc

    @app.post("/settle", response_model=SettleResponse)
    @limiter.limit("20/minute", key_func=_payment_rate_limit_key)
    async def settle(
        request: Request,
        body: PaymentRequest,
    ) -> SettleResponse:
        _require_authenticated_gateway(request)
        if not body.signed_tx_blob:
            raise HTTPException(status_code=400, detail="signed_tx_blob required")
        try:
            return await active_xrpl_service.settle_payment(body.signed_tx_blob, body.invoice_id)
        except ValueError as exc:
            logger.error("settlement_failed", error=str(exc))
            raise HTTPException(status_code=402, detail=f"Settlement failed: {exc}") from exc

    return app

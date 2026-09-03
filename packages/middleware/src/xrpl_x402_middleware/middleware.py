from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import ASGIApp
from x402 import x402ResourceServer
from x402.extensions.bazaar import bazaar_resource_server_extension
from x402.extensions.payment_identifier import (
    PAYMENT_IDENTIFIER,
    declare_payment_identifier_extension,
    extract_and_validate_payment_identifier,
    is_payment_identifier_required,
    payment_identifier_resource_server_extension,
    validate_payment_identifier_requirement,
)
from x402.http import (
    PAYMENT_RESPONSE_HEADER,
    PaymentOption,
    RouteConfig,
    decode_payment_response_header,
    x402HTTPResourceServer,
)
from x402.http.middleware import PaymentMiddlewareASGI as UpstreamPaymentMiddlewareASGI
from x402.schemas import (
    AbortResult,
    AssetAmount,
    RecoveredVerifyResult,
    ResourceInfo,
    SkipHandlerDirective,
    SkipHandlerResult,
    VerifyResponse,
)

from xrpl_x402_middleware.adapters.x402 import register_exact_xrpl_server
from xrpl_x402_middleware.client import build_facilitator_client
from xrpl_x402_middleware.response_store import (
    AuthoritativeResource,
    RedisResourceResponseStore,
    StoredResourceResponse,
)
from xrpl_x402_middleware.types import (
    AcceptedRequirementsContext,
    XRPLPaymentContext,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CACHED_RESPONSE_STATE = "_x402_cached_resource_response"
_AUTHORITATIVE_RESOURCE_STATE = "_x402_authoritative_resource"
_PAYMENT_IDENTIFIER_CONFLICT_STATE = "_x402_payment_identifier_conflict"
_STALE_AUTHORIZATION_FAILURES = frozenset({
    "invalid_exact_xrpl_payload_sequence_not_current",
    "invalid_exact_xrpl_payload_ticket_not_available",
})


def create_resource_server(
    facilitator_client: Any,
    *,
    response_store: RedisResourceResponseStore | None = None,
    enable_bazaar: bool = True,
) -> x402ResourceServer:
    server = register_exact_xrpl_server(x402ResourceServer(facilitator_client))
    server.register_extension(payment_identifier_resource_server_extension)
    if enable_bazaar:
        server.register_extension(bazaar_resource_server_extension)

    _register_authoritative_resource_hook(server)

    async def require_declared_payment_identifier(context: Any) -> AbortResult | None:
        if not context.result.is_valid:
            return None
        declared = context.declared_extensions or {}
        declaration = declared.get(PAYMENT_IDENTIFIER)
        if not is_payment_identifier_required(declaration):
            return None

        requirement_validation = validate_payment_identifier_requirement(
            context.payment_payload,
            server_required=True,
        )
        if not requirement_validation.valid:
            return AbortResult(
                reason="invalid_payment_identifier",
                message="; ".join(requirement_validation.errors),
            )

        identifier, identifier_validation = (
            extract_and_validate_payment_identifier(context.payment_payload)
        )
        if not identifier_validation.valid or identifier is None:
            errors = identifier_validation.errors or [
                "Server requires a valid payment identifier"
            ]
            return AbortResult(
                reason="invalid_payment_identifier",
                message="; ".join(errors),
            )
        return None

    server.on_after_verify(require_declared_payment_identifier)

    if response_store is not None:
        async def recover_cached_authorization(
            context: Any,
        ) -> RecoveredVerifyResult | None:
            if not _is_stale_authorization_failure(str(context.error)):
                return None
            authoritative = _authoritative_resource_from_transport(
                context.transport_context
            )
            if not _requires_response_recovery(authoritative):
                return None
            try:
                cached = await response_store.get(
                    context.payment_payload,
                    context.requirements,
                    authoritative_resource=authoritative,
                )
            except ValueError as error:
                _record_payment_identifier_conflict(context, str(error))
                return None
            if cached is None:
                return None
            return RecoveredVerifyResult(
                result=VerifyResponse(
                    is_valid=True,
                    extra={
                        "verificationPath": "cached_response_reconciliation"
                    },
                )
            )

        server.on_verify_failure(recover_cached_authorization)

        async def recover_handler(
            context: Any,
        ) -> AbortResult | SkipHandlerResult | None:
            if not context.result.is_valid:
                return None
            authoritative = _authoritative_resource_from_transport(
                context.transport_context
            )
            if not _requires_response_recovery(authoritative):
                return None
            try:
                cached = await response_store.get(
                    context.payment_payload,
                    context.requirements,
                    authoritative_resource=authoritative,
                )
            except ValueError as error:
                _record_payment_identifier_conflict(context, str(error))
                return AbortResult(
                    reason="payment_identifier_conflict",
                    message=str(error),
                )
            if cached is None:
                try:
                    acquired = await response_store.bind(
                        context.payment_payload,
                        context.requirements,
                        authoritative_resource=authoritative,
                    )
                except ValueError as error:
                    _record_payment_identifier_conflict(context, str(error))
                    return AbortResult(
                        reason="payment_identifier_conflict",
                        message=str(error),
                    )
                if acquired:
                    return None
                return AbortResult(
                    reason="payment_identifier_in_progress",
                    message="A matching paid request is already executing",
                )
            transport = context.transport_context
            adapter = getattr(getattr(transport, "request", None), "adapter", None)
            request = getattr(adapter, "_request", None)
            if request is not None:
                setattr(request.state, _CACHED_RESPONSE_STATE, cached)
            return SkipHandlerResult(
                response=SkipHandlerDirective(
                    content_type=cached.content_type,
                    body={} if isinstance(cached.body, bytes) else cached.body,
                )
            )

        server.on_after_verify(recover_handler)
    return server


class PaymentMiddlewareASGI(UpstreamPaymentMiddlewareASGI):
    """Canonical x402 HTTP middleware with XRPL recovery semantics."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        routes: Mapping[str, RouteConfig | dict[str, Any]] | None = None,
        route_configs: Mapping[str, RouteConfig | dict[str, Any]] | None = None,
        server: x402ResourceServer | None = None,
        facilitator_client: Any = None,
        facilitator_url: str | None = None,
        bearer_token: str | None = None,
        response_store: RedisResourceResponseStore | None = None,
        enable_bazaar: bool = True,
    ) -> None:
        selected_routes = dict(routes or route_configs or {})
        if not selected_routes:
            raise ValueError("routes cannot be empty")
        if response_store is None and any(
            pattern.partition(" ")[0].upper() not in SAFE_METHODS
            for pattern in selected_routes
        ):
            raise ValueError(
                "unsafe paid routes require a RedisResourceResponseStore"
            )
        normalized_routes = _require_identifiers_for_unsafe_routes(selected_routes)
        if server is None:
            if facilitator_client is None:
                if not facilitator_url or not bearer_token:
                    raise ValueError(
                        "facilitator_url and bearer_token are required when server is omitted"
                    )
                facilitator_client = build_facilitator_client(
                    base_url=facilitator_url,
                    bearer_token=bearer_token,
                )
            server = create_resource_server(
                facilitator_client,
                response_store=response_store,
                enable_bazaar=enable_bazaar,
            )
        _register_authoritative_resource_hook(server)
        self._route_resolver = x402HTTPResourceServer(
            server, normalized_routes
        )
        self._response_store = response_store
        super().__init__(app, routes=normalized_routes, server=server)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Response:
        authoritative = await _resolve_http_authoritative_resource(
            request, self._route_resolver
        )
        if authoritative is not None:
            setattr(request.state, _AUTHORITATIVE_RESOURCE_STATE, authoritative)

        async def buffered_call_next(inner_request: Request) -> Response:
            response = await call_next(inner_request)
            if response.status_code >= 400:
                return response
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            rebuilt = StreamingResponse(
                iter([body]),
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
            if (
                self._response_store is not None
                and inner_request.method.upper() not in SAFE_METHODS
                and hasattr(inner_request.state, "payment_payload")
                and hasattr(inner_request.state, "payment_requirements")
            ):
                authoritative = getattr(
                    inner_request.state, _AUTHORITATIVE_RESOURCE_STATE, None
                )
                if authoritative is None:
                    raise RuntimeError("authoritative HTTP resource is unavailable")
                cached = _to_stored_response(rebuilt, body)
                await self._response_store.put(
                    inner_request.state.payment_payload,
                    inner_request.state.payment_requirements,
                    cached,
                    authoritative_resource=authoritative,
                )
            return rebuilt

        response = await super().dispatch(request, buffered_call_next)
        conflict = getattr(
            request.state, _PAYMENT_IDENTIFIER_CONFLICT_STATE, None
        )
        if conflict is not None:
            return JSONResponse(
                {"error": conflict},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        cached = getattr(request.state, _CACHED_RESPONSE_STATE, None)
        if cached is not None and 200 <= response.status_code < 400:
            response = _restore_cached_response(response, cached)

        raw_settlement = response.headers.get(PAYMENT_RESPONSE_HEADER)
        requirements = getattr(request.state, "payment_requirements", None)
        if raw_settlement and requirements is not None:
            settlement = decode_payment_response_header(raw_settlement)
            request.state.x402_payment = XRPLPaymentContext(
                settlement=settlement,
                accepted=AcceptedRequirementsContext.from_requirements(requirements),
            )
        return response


def require_payment(
    *,
    pay_to: str,
    network: str,
    amount: str | None = None,
    xrp_drops: int | None = None,
    asset: str = "XRP",
    issuer: str | None = None,
    max_timeout_seconds: int = 60,
    asset_transfer_method: str = "sequence",
    invoice_id: str | None = None,
    destination_tag: int | None = None,
    resource: str | None = None,
    description: str | None = None,
    mime_type: str = "application/json",
    service_name: str | None = None,
    tags: list[str] | None = None,
    icon_url: str | None = None,
    extensions: dict[str, Any] | None = None,
) -> RouteConfig:
    if (amount is None) == (xrp_drops is None):
        raise ValueError("provide exactly one of amount or xrp_drops")
    if asset == "XRP":
        if xrp_drops is None or issuer is not None:
            raise ValueError("XRP pricing requires xrp_drops and no issuer")
        price_amount = str(xrp_drops)
    else:
        if amount is None or not issuer:
            raise ValueError("XRPL IOU pricing requires amount and issuer")
        price_amount = amount
    extra: dict[str, Any] = {
        "areFeesSponsored": False,
        "assetTransferMethod": asset_transfer_method,
    }
    if issuer:
        extra["issuer"] = issuer
    if invoice_id is not None:
        extra["invoiceId"] = invoice_id
    if destination_tag is not None:
        extra["destinationTag"] = destination_tag
    option = PaymentOption(
        scheme="exact",
        pay_to=pay_to,
        price=AssetAmount(amount=price_amount, asset=asset, extra=extra),
        network=network,
        max_timeout_seconds=max_timeout_seconds,
    )
    return RouteConfig(
        accepts=option,
        resource=resource,
        description=description,
        mime_type=mime_type,
        service_name=service_name,
        tags=tags,
        icon_url=icon_url,
        extensions=dict(extensions or {}),
    )


def _require_identifiers_for_unsafe_routes(
    routes: Mapping[str, RouteConfig | dict[str, Any]],
) -> dict[str, RouteConfig | dict[str, Any]]:
    normalized: dict[str, RouteConfig | dict[str, Any]] = {}
    for pattern, config in routes.items():
        method = pattern.partition(" ")[0].upper()
        if method in SAFE_METHODS:
            normalized[pattern] = config
            continue
        declaration = declare_payment_identifier_extension(required=True)
        if isinstance(config, RouteConfig):
            extensions = dict(config.extensions or {})
            extensions["payment-identifier"] = declaration
            normalized[pattern] = RouteConfig(
                accepts=config.accepts,
                resource=config.resource,
                description=config.description,
                mime_type=config.mime_type,
                service_name=config.service_name,
                tags=config.tags,
                icon_url=config.icon_url,
                custom_paywall_html=config.custom_paywall_html,
                unpaid_response_body=config.unpaid_response_body,
                settlement_failed_response_body=config.settlement_failed_response_body,
                extensions=extensions,
                hook_timeout_seconds=config.hook_timeout_seconds,
            )
        else:
            copied = dict(config)
            extensions = dict(copied.get("extensions") or {})
            extensions["payment-identifier"] = declaration
            copied["extensions"] = extensions
            normalized[pattern] = copied
    return normalized


async def _resolve_http_authoritative_resource(
    request: Request,
    route_resolver: x402HTTPResourceServer,
) -> AuthoritativeResource | None:
    raw_path = request.scope["raw_path"].decode("ascii").split("?")[0]
    route_match = route_resolver._get_route_config(raw_path, request.method)
    if route_match is None:
        return None
    route_config, _pattern = route_match
    resource = ResourceInfo(
        url=route_config.resource or str(request.url),
        description=route_config.description or "",
        mime_type=route_config.mime_type or "",
        service_name=route_config.service_name,
        tags=route_config.tags,
        icon_url=route_config.icon_url,
    )
    method = request.method.upper()
    body = await request.body() if method not in SAFE_METHODS else b""
    invocation = json.dumps(
        {
            "bodySha256": hashlib.sha256(body).hexdigest(),
            "method": method,
            "url": str(request.url),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    invocation_digest = hashlib.sha256(invocation.encode("utf-8")).hexdigest()
    return AuthoritativeResource(
        resource=resource,
        identity=f"http:{method}:{invocation_digest}",
    )


def _authoritative_resource_from_transport(
    transport_context: Any,
) -> AuthoritativeResource | None:
    adapter = getattr(
        getattr(transport_context, "request", None), "adapter", None
    )
    request = getattr(adapter, "_request", None)
    if request is None:
        return None
    return getattr(request.state, _AUTHORITATIVE_RESOURCE_STATE, None)


def _requires_response_recovery(
    authoritative: AuthoritativeResource | None,
) -> bool:
    if authoritative is None or not authoritative.identity.startswith("http:"):
        return False
    _prefix, method, _resource = authoritative.identity.split(":", 2)
    return method not in SAFE_METHODS


def _is_stale_authorization_failure(reason: str | None) -> bool:
    return reason in _STALE_AUTHORIZATION_FAILURES


def _record_payment_identifier_conflict(
    context: Any,
    message: str,
) -> None:
    transport = getattr(context, "transport_context", None)
    adapter = getattr(getattr(transport, "request", None), "adapter", None)
    request = getattr(adapter, "_request", None)
    if request is not None:
        setattr(
            request.state,
            _PAYMENT_IDENTIFIER_CONFLICT_STATE,
            message,
        )


async def _require_authoritative_resource(context: Any) -> AbortResult | None:
    authoritative = _authoritative_resource_from_transport(
        context.transport_context
    )
    if authoritative is None:
        return None
    error = _bind_payload_to_authoritative_resource(
        context.payment_payload, authoritative
    )
    if error is not None:
        return AbortResult(
            reason="resource_mismatch",
            message=error,
        )
    return None


def _register_authoritative_resource_hook(server: x402ResourceServer) -> None:
    marker = "_xrpl_x402_authoritative_resource_hook"
    if getattr(server, marker, False):
        return
    server.on_before_verify(_require_authoritative_resource)
    setattr(server, marker, True)


def _bind_payload_to_authoritative_resource(
    payload: Any,
    authoritative: AuthoritativeResource,
) -> str | None:
    supplied = getattr(payload, "resource", None)
    if supplied is None:
        return "payment payload omitted the authoritative resource"
    supplied_wire = supplied.model_dump(by_alias=True, exclude_none=True)
    authoritative_wire = authoritative.resource.model_dump(
        by_alias=True, exclude_none=True
    )
    if supplied_wire != authoritative_wire:
        return "payment payload resource does not match the protected resource"
    payload.resource = authoritative.resource.model_copy(deep=True)
    return None


def _to_stored_response(response: Response, body: bytes) -> StoredResourceResponse:
    content_type = response.headers.get("content-type", "application/octet-stream")
    return StoredResourceResponse(
        status_code=response.status_code,
        content_type=content_type,
        body=bytes(body),
        headers={
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "payment-response"}
        },
    )


def _restore_cached_response(
    settled_response: Response,
    cached: StoredResourceResponse,
) -> Response:
    headers = {
        key: value
        for key, value in settled_response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    for key, value in cached.headers.items():
        if key.lower() not in {
            "cache-control",
            "content-length",
            "content-type",
            "payment-response",
        }:
            headers[key] = value
    headers["content-type"] = cached.content_type
    return Response(
        content=_stored_body_bytes(cached),
        status_code=cached.status_code,
        headers=headers,
    )


def _stored_body_bytes(cached: StoredResourceResponse) -> bytes:
    if isinstance(cached.body, bytes):
        return cached.body
    if isinstance(cached.body, str):
        return cached.body.encode("utf-8")
    return json.dumps(
        cached.body,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "PaymentMiddlewareASGI",
    "create_resource_server",
    "require_payment",
]

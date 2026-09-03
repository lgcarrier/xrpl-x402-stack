from __future__ import annotations

import hashlib
import inspect
import json
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable

from x402.extensions.payment_identifier import declare_payment_identifier_extension
from x402.mcp import (
    MCP_PAYMENT_RESPONSE_META_KEY,
    PaymentWrapperHooks,
    create_payment_wrapper,
)
from x402.schemas import (
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
    VerifyResponse,
)

from xrpl_x402_middleware.middleware import (
    _bind_payload_to_authoritative_resource,
    _is_stale_authorization_failure,
    create_resource_server,
)
from xrpl_x402_middleware.response_store import (
    AuthoritativeResource,
    RedisResourceResponseStore,
    StoredResourceResponse,
)


class _SettlementCapturingResourceServer:
    """Delegate to the upstream server while retaining its settlement result."""

    def __init__(
        self,
        server: Any,
        settlement_context: ContextVar[SettleResponse | None],
        resource_context: ContextVar[AuthoritativeResource | None],
        response_store: RedisResourceResponseStore | None,
    ) -> None:
        self._server = server
        self._settlement_context = settlement_context
        self._resource_context = resource_context
        self._response_store = response_store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    async def verify_payment(
        self,
        payload: Any,
        requirements: PaymentRequirements,
        *args: Any,
        **kwargs: Any,
    ) -> VerifyResponse:
        authoritative = self._resource_context.get()
        if authoritative is not None:
            error = _bind_payload_to_authoritative_resource(
                payload, authoritative
            )
            if error is not None:
                return VerifyResponse(
                    is_valid=False,
                    invalid_reason="resource_mismatch",
                    invalid_message=error,
                )
        result = self._server.verify_payment(
            payload, requirements, *args, **kwargs
        )
        if inspect.isawaitable(result):
            result = await result
        if (
            not result.is_valid
            and self._response_store is not None
            and authoritative is not None
            and _is_stale_authorization_failure(result.invalid_reason)
        ):
            try:
                cached = await self._response_store.get(
                    payload,
                    requirements,
                    authoritative_resource=authoritative,
                )
            except ValueError:
                cached = None
            if cached is not None:
                return VerifyResponse(
                    is_valid=True,
                    payer=getattr(result, "payer", None),
                    extra={
                        "verificationPath": "cached_response_reconciliation"
                    },
                )
        return result

    async def settle_payment(
        self,
        payload: Any,
        requirements: PaymentRequirements,
        *args: Any,
        **kwargs: Any,
    ) -> SettleResponse:
        authoritative = self._resource_context.get()
        if authoritative is not None:
            error = _bind_payload_to_authoritative_resource(
                payload, authoritative
            )
            if error is not None:
                return SettleResponse(
                    success=False,
                    error_reason="resource_mismatch",
                    error_message=error,
                    transaction="",
                    network=requirements.network,
                )
        result = self._server.settle_payment(
            payload, requirements, *args, **kwargs
        )
        if inspect.isawaitable(result):
            result = await result
        self._settlement_context.set(result)
        return result


def _preserve_pending_settlement_response(
    handler: Callable,
    settlement_context: ContextVar[SettleResponse | None],
    resource_context: ContextVar[AuthoritativeResource | None],
    authoritative_resource: AuthoritativeResource,
) -> Callable:
    """Restore the standard response discarded by x402 2.20.0's MCP wrapper."""

    @wraps(handler)
    async def wrapped(**kwargs: Any) -> Any:
        token = settlement_context.set(None)
        invocation_resource = _mcp_invocation_resource(
            authoritative_resource, kwargs
        )
        resource_token = resource_context.set(invocation_resource)
        try:
            result = await handler(**kwargs)
            settlement = settlement_context.get()
            structured_content = getattr(result, "structuredContent", None)
            if (
                settlement is not None
                and not settlement.success
                and settlement.error_reason == "settlement_pending"
                and getattr(result, "isError", False)
                and isinstance(structured_content, dict)
            ):
                pending_wire = settlement.model_dump(
                    by_alias=True, exclude_none=True
                )
                structured_content = dict(structured_content)
                structured_content[MCP_PAYMENT_RESPONSE_META_KEY] = (
                    pending_wire
                )
                result.structuredContent = structured_content
                if result.content and getattr(result.content[0], "type", None) == "text":
                    result.content[0].text = json.dumps(structured_content)
                raw_meta = dict(getattr(result, "meta", None) or {})
                raw_meta[MCP_PAYMENT_RESPONSE_META_KEY] = pending_wire
                result.meta = raw_meta
                setattr(result, "_meta", raw_meta)
            return result
        finally:
            resource_context.reset(resource_token)
            settlement_context.reset(token)

    return wrapped


def create_xrpl_mcp_payment_wrapper(
    facilitator_client: Any,
    *,
    accepts: list[PaymentRequirements],
    resource: ResourceInfo | None = None,
    extensions: dict[str, Any] | None = None,
    non_idempotent: bool = True,
    enable_bazaar: bool = True,
    response_store: RedisResourceResponseStore | None = None,
) -> Callable:
    """Create the official MCP payment wrapper with XRPL mechanisms registered.

    Payment identifiers are required by default because most MCP tools may have
    side effects. Set ``non_idempotent=False`` only for a demonstrably read-only
    tool.
    """

    if not accepts:
        raise ValueError("accepts cannot be empty")
    if non_idempotent and response_store is None:
        raise ValueError(
            "non-idempotent MCP tools require a RedisResourceResponseStore"
        )
    wrapper_extensions = dict(extensions or {})
    if non_idempotent:
        wrapper_extensions["payment-identifier"] = (
            declare_payment_identifier_extension(required=True)
        )
    server = create_resource_server(
        facilitator_client,
        enable_bazaar=enable_bazaar,
    )
    server.initialize()
    settlement_context: ContextVar[SettleResponse | None] = ContextVar(
        "xrpl_x402_mcp_settlement", default=None
    )
    resource_context: ContextVar[AuthoritativeResource | None] = ContextVar(
        "xrpl_x402_mcp_resource", default=None
    )
    capturing_server = _SettlementCapturingResourceServer(
        server,
        settlement_context,
        resource_context,
        response_store if non_idempotent else None,
    )
    if response_store is None or not non_idempotent:
        official_wrapper = create_payment_wrapper(
            capturing_server,
            accepts=accepts,
            resource=resource,
            extensions=wrapper_extensions,
        )

        def settlement_preserving_wrapper(handler: Callable) -> Callable:
            authoritative = _mcp_authoritative_resource(handler, resource)
            return _preserve_pending_settlement_response(
                official_wrapper(handler),
                settlement_context,
                resource_context,
                authoritative,
            )

        return settlement_preserving_wrapper

    recovery_context: ContextVar[
        tuple[
            Any,
            PaymentRequirements,
            StoredResourceResponse | None,
            AuthoritativeResource,
        ]
        | None
    ] = ContextVar("xrpl_x402_mcp_recovery", default=None)

    async def load_cached_response(context: Any) -> bool:
        recovery_context.set(None)
        authoritative = resource_context.get()
        if authoritative is None:
            return False
        try:
            cached = await response_store.get(
                context.payment_payload,
                context.payment_requirements,
                authoritative_resource=authoritative,
            )
            if cached is None:
                acquired = await response_store.bind(
                    context.payment_payload,
                    context.payment_requirements,
                    authoritative_resource=authoritative,
                )
                if not acquired:
                    return False
        except ValueError:
            return False
        recovery_context.set(
            (
                context.payment_payload,
                context.payment_requirements,
                cached,
                authoritative,
            )
        )
        return True

    official_wrapper = create_payment_wrapper(
        capturing_server,
        accepts=accepts,
        resource=resource,
        hooks=PaymentWrapperHooks(on_before_execution=load_cached_response),
        extensions=wrapper_extensions,
    )

    def recovery_wrapper(handler: Callable) -> Callable:
        @wraps(handler)
        async def recoverable(**kwargs: Any) -> Any:
            state = recovery_context.get()
            if state is None:
                raise RuntimeError("MCP payment recovery context is unavailable")
            payload, requirements, cached, authoritative = state
            try:
                if cached is not None:
                    return cached.body
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, (dict, str)):
                    raise TypeError(
                        "recoverable paid MCP tools must return a dict or string"
                    )
                await response_store.put(
                    payload,
                    requirements,
                    StoredResourceResponse(
                        status_code=200,
                        content_type=(
                            "application/json"
                            if isinstance(result, dict)
                            else "text/plain"
                        ),
                        body=result,
                        headers={},
                    ),
                    authoritative_resource=authoritative,
                )
                return result
            finally:
                recovery_context.set(None)

        authoritative = _mcp_authoritative_resource(handler, resource)
        return _preserve_pending_settlement_response(
            official_wrapper(recoverable),
            settlement_context,
            resource_context,
            authoritative,
        )

    return recovery_wrapper


def _mcp_invocation_resource(
    authoritative: AuthoritativeResource,
    kwargs: dict[str, Any],
) -> AuthoritativeResource:
    arguments = {
        key: value
        for key, value in kwargs.items()
        if key != "ctx"
    }
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return AuthoritativeResource(
        resource=authoritative.resource,
        identity=f"{authoritative.identity}:{digest}",
    )


def _mcp_authoritative_resource(
    handler: Callable,
    configured: ResourceInfo | None,
) -> AuthoritativeResource:
    tool_name = handler.__name__
    resource = configured.model_copy(deep=True) if configured else ResourceInfo(
        url=f"mcp://tool/{tool_name}",
        description=f"Tool: {tool_name}",
        mime_type="application/json",
    )
    return AuthoritativeResource(
        resource=resource,
        identity=f"mcp:{tool_name}",
    )


__all__ = ["ResourceInfo", "create_xrpl_mcp_payment_wrapper"]

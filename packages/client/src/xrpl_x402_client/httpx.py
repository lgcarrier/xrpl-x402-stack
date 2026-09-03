from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from x402 import x402Client
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    decode_payment_required_header,
    decode_payment_response_header,
)
from x402.http.clients.httpx import x402AsyncTransport
from x402.http.x402_http_client import x402HTTPClient
from x402.http.x402_http_client_base import ProcessPaymentResult
from x402.schemas import PaymentRequired, SettleResponse

from xrpl_x402_client.signer import (
    XRPLPaymentSigner,
    register_exact_xrpl_client,
)
from xrpl_x402_client.spend import (
    XRPLAssetSpendLimit,
    apply_xrpl_spend_limits,
)


class CanonicalXRPLHTTPClient(x402HTTPClient):
    """V2-only HTTP adapter with a single protected-request retry."""

    def get_payment_required_response(
        self,
        get_header: Callable[[str], str | None],
        body: Any = None,
    ) -> PaymentRequired:
        del body
        raw = get_header(PAYMENT_REQUIRED_HEADER)
        if not raw:
            raise ValueError(
                "Only canonical x402 v2 PAYMENT-REQUIRED headers are supported"
            )
        parsed = decode_payment_required_header(raw)
        if not isinstance(parsed, PaymentRequired) or parsed.x402_version != 2:
            raise ValueError(
                "Only canonical x402 v2 PAYMENT-REQUIRED headers are supported"
            )
        return parsed

    def get_payment_settle_response(
        self,
        get_header: Callable[[str], str | None],
    ) -> SettleResponse:
        raw = get_header(PAYMENT_RESPONSE_HEADER)
        if not raw:
            raise ValueError(
                "Only canonical x402 v2 PAYMENT-RESPONSE headers are supported"
            )
        parsed = decode_payment_response_header(raw)
        if not isinstance(parsed, SettleResponse):
            raise ValueError("Invalid canonical x402 v2 settlement response")
        return parsed

    async def process_payment_result(
        self,
        payment_payload: Any,
        get_header: Callable[[str], str | None],
        status: int,
    ) -> ProcessPaymentResult:
        result = await super().process_payment_result(
            payment_payload, get_header, status
        )
        # Settlement reconciliation happens inside the facilitator client. Never
        # issue a second protected-resource retry with a newly signed payload.
        return ProcessPaymentResult(
            recovered=False,
            settle_response=result.settle_response,
        )


class XRPLPaymentTransport(x402AsyncTransport):
    """Official x402 httpx transport preconfigured with XRPL exact."""

    def __init__(
        self,
        signer: XRPLPaymentSigner,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        networks: str | list[str] | None = None,
        spend_controls: dict[str, Any] | bool | None = None,
        asset_limits: list[XRPLAssetSpendLimit] | None = None,
        client: x402Client | None = None,
    ) -> None:
        payment_client = client or x402Client()
        if client is None:
            register_exact_xrpl_client(
                payment_client, signer, networks or signer.network
            )
            if asset_limits:
                apply_xrpl_spend_limits(payment_client, asset_limits)
            elif spend_controls is not None:
                payment_client.set_spend_controls(spend_controls)
        super().__init__(CanonicalXRPLHTTPClient(payment_client), transport)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Upstream buffers every body before the first send so it can retry a
        # 402. A custom async stream may be one-shot or unbounded, so sending it
        # through that path could consume unbounded memory or duplicate an
        # upload. Only httpx's immutable in-memory ByteStream is known to be
        # replayable here; all other streams are sent exactly once.
        if not isinstance(request.stream, httpx.ByteStream):
            return await self._transport.handle_async_request(request)
        return await super().handle_async_request(request)


def wrap_httpx_with_xrpl_payment(
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    spend_controls: dict[str, Any] | bool | None = None,
    asset_limits: list[XRPLAssetSpendLimit] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=XRPLPaymentTransport(
            signer,
            transport=transport,
            networks=network,
            spend_controls=spend_controls,
            asset_limits=asset_limits,
        ),
        **client_kwargs,
    )

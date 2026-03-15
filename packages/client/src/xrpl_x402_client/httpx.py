from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx

from xrpl_x402_client.signer import (
    PAYMENT_SIGNATURE_HEADER,
    XRPLPaymentSigner,
    build_payment_signature,
    decode_payment_required_response,
)


class XRPLPaymentTransport(httpx.AsyncBaseTransport):
    RETRY_KEY = "_xrpl_x402_retry"

    def __init__(
        self,
        signer: XRPLPaymentSigner,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        network: str | None = None,
        asset: str | None = None,
        invoice_id_factory: Callable[[], str | None] | None = None,
    ) -> None:
        self._signer = signer
        self._transport = transport or httpx.AsyncHTTPTransport()
        self._network = network
        self._asset = asset
        self._invoice_id_factory = invoice_id_factory

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        if response.status_code != 402:
            return response

        if request.extensions.get(self.RETRY_KEY):
            return response

        await response.aread()
        payment_required = decode_payment_required_response(
            headers=dict(response.headers),
            body=response.content,
        )
        invoice_id = self._invoice_id_factory() if self._invoice_id_factory else None
        payment_signature = await asyncio.to_thread(
            build_payment_signature,
            payment_required,
            self._signer,
            network=self._network,
            asset=self._asset,
            invoice_id=invoice_id,
        )

        retry_headers = dict(request.headers)
        retry_headers[PAYMENT_SIGNATURE_HEADER] = payment_signature
        retry_request = httpx.Request(
            method=request.method,
            url=request.url,
            headers=retry_headers,
            content=request.content,
            extensions={**request.extensions, self.RETRY_KEY: True},
        )
        return await self._transport.handle_async_request(retry_request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def wrap_httpx_with_xrpl_payment(
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    asset: str | None = None,
    invoice_id_factory: Callable[[], str | None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=XRPLPaymentTransport(
            signer,
            transport=transport,
            network=network,
            asset=asset,
            invoice_id_factory=invoice_id_factory,
        ),
        **client_kwargs,
    )

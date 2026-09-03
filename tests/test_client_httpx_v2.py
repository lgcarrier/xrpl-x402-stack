from __future__ import annotations

import asyncio
import httpx
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner, XRPLPaymentTransport


class OneShotBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.iterations = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("one-shot request body was replayed")
        yield b"streamed-body"


class ChallengeTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0
        self.bodies: list[bytes] = []

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        self.calls += 1
        self.bodies.append(await request.aread())
        return httpx.Response(402, request=request)


def test_non_replayable_stream_is_never_buffered_or_payment_retried() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=1014,
    )
    body = OneShotBody()
    inner = ChallengeTransport()

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=XRPLPaymentTransport(
                signer,
                transport=inner,
                spend_controls=False,
            )
        ) as client:
            return await client.post(
                "https://merchant.example/upload",
                content=body,
            )

    response = asyncio.run(send())

    assert response.status_code == 402
    assert inner.calls == 1
    assert inner.bodies == [b"streamed-body"]
    assert body.iterations == 1

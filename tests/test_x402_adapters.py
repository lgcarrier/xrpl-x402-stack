from __future__ import annotations

import asyncio
import json

import httpx
from xrpl.core import binarycodec
from xrpl.wallet import Wallet

from x402 import x402ClientSync, x402ResourceServer
from x402.schemas import AssetAmount, PaymentRequired, PaymentRequirements, ResourceConfig

from xrpl_x402_client import XRPLPaymentSigner
from xrpl_x402_client.adapters.x402 import register_exact_xrpl_client
from xrpl_x402_middleware.adapters.x402 import XRPLX402FacilitatorClient, register_exact_xrpl_server

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rPAYER123456789ABCDEFG"


def test_register_exact_xrpl_client_creates_x402_payment_payload() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    client = register_exact_xrpl_client(x402ClientSync(), signer)
    requirements = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP:native",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=300,
    )
    payment_required = PaymentRequired(
        error="Pay to continue",
        accepts=[requirements],
    )

    payload = client.create_payment_payload(payment_required)
    tx = binarycodec.decode(payload.payload["signedTxBlob"])

    assert payload.accepted.network == "xrpl:1"
    assert tx["Destination"] == DESTINATION
    assert tx["Amount"] == "1000"


def test_x402_resource_server_interops_with_xrpl_facilitator_adapter() -> None:
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    x402_client = register_exact_xrpl_client(x402ClientSync(), signer)
    recorded_requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/supported":
            return httpx.Response(
                200,
                json={
                    "network": "xrpl:1",
                    "assets": [{"code": "XRP"}],
                    "settlement_mode": "validated",
                },
            )

        payload = json.loads(request.content.decode("utf-8"))
        recorded_requests.append((request.url.path, payload))
        if request.url.path == "/verify":
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "invoice_id": "AUTO-INVOICE",
                    "amount": "0.001 XRP",
                    "asset": {"code": "XRP"},
                    "amount_details": {
                        "value": "1000",
                        "unit": "drops",
                        "asset": {"code": "XRP"},
                        "drops": 1000,
                    },
                    "payer": PAYER,
                    "destination": DESTINATION,
                    "message": "Payment valid",
                },
            )

        return httpx.Response(
            200,
            json={
                "settled": True,
                "tx_hash": "TX-123",
                "status": "validated",
            },
        )

    transport = httpx.MockTransport(handler)
    sync_client = httpx.Client(transport=transport, base_url="https://facilitator.example")
    async_client = httpx.AsyncClient(transport=transport, base_url="https://facilitator.example")
    facilitator_client = XRPLX402FacilitatorClient(
        base_url="https://facilitator.example",
        bearer_token="secret-token",
        sync_client=sync_client,
        async_client=async_client,
    )
    server = register_exact_xrpl_server(x402ResourceServer(facilitator_client))
    server.initialize()
    requirements = server.build_payment_requirements(
        ResourceConfig(
            scheme="exact",
            network="xrpl:1",
            pay_to=DESTINATION,
            price=AssetAmount(amount="1000", asset="XRP:native"),
        )
    )
    payload = x402_client.create_payment_payload(
        PaymentRequired(
            error="Pay",
            accepts=requirements,
        )
    )

    async def _run() -> tuple[object, object]:
        verify_result = await server.verify_payment(payload, requirements[0])
        settle_result = await server.settle_payment(payload, requirements[0])
        await facilitator_client.aclose()
        return verify_result, settle_result

    verify_result, settle_result = asyncio.run(_run())
    sync_client.close()

    assert verify_result.is_valid is True
    assert verify_result.payer == PAYER
    assert settle_result.success is True
    assert settle_result.transaction == "TX-123"
    assert [path for path, _payload in recorded_requests] == ["/verify", "/settle"]
    assert recorded_requests[0][1]["signed_tx_blob"] == payload.payload["signedTxBlob"]

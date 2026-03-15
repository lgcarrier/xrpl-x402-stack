from __future__ import annotations

import asyncio

import httpx
from xrpl.core import binarycodec
from xrpl.wallet import Wallet

from xrpl_x402_client import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    XRPLPaymentSigner,
    build_payment_signature,
    decode_payment_required,
    select_payment_option,
    wrap_httpx_with_xrpl_payment,
)
from xrpl_x402_core import (
    PaymentPayload,
    PaymentRequired,
    XRPLPaymentOption,
    decode_model_from_base64,
    encode_model_to_base64,
)

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


def _payment_required() -> PaymentRequired:
    return PaymentRequired(
        error="Payment required",
        accepts=[
            XRPLPaymentOption(
                network="xrpl:0",
                payTo=DESTINATION,
                maxAmountRequired="2500",
                asset={"code": "XRP"},
                amount={"value": "2500", "unit": "drops", "drops": 2500},
            ),
            XRPLPaymentOption(
                network="xrpl:1",
                payTo=DESTINATION,
                maxAmountRequired="1000",
                asset={"code": "XRP"},
                amount={"value": "1000", "unit": "drops", "drops": 1000},
            ),
        ],
    )


def test_decode_payment_required_round_trips_header() -> None:
    challenge = _payment_required()

    decoded = decode_payment_required(encode_model_to_base64(challenge))

    assert decoded == challenge


def test_select_payment_option_filters_by_network() -> None:
    challenge = _payment_required()

    option = select_payment_option(challenge, network="xrpl:1")

    assert option.network == "xrpl:1"
    assert option.amount.value == "1000"


def test_build_payment_signature_signs_offline_exact_xrp_payment() -> None:
    option = select_payment_option(_payment_required(), network="xrpl:1")
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )

    payment_signature = build_payment_signature(option, signer)
    payload = decode_model_from_base64(payment_signature, PaymentPayload)
    tx = binarycodec.decode(payload.payload.signed_tx_blob)

    assert payload.network == "xrpl:1"
    assert tx["Destination"] == DESTINATION
    assert tx["Account"] == signer.wallet.classic_address
    assert tx["Amount"] == "1000"


def test_httpx_transport_retries_once_after_402() -> None:
    challenge = _payment_required()
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    attempts = 0
    captured_signature: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts, captured_signature
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                402,
                headers={PAYMENT_REQUIRED_HEADER: encode_model_to_base64(challenge)},
                json=challenge.model_dump(by_alias=True, exclude_none=True),
            )

        captured_signature = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        return httpx.Response(200, json={"ok": True})

    async def _run() -> httpx.Response:
        async with wrap_httpx_with_xrpl_payment(
            signer,
            transport=httpx.MockTransport(handler),
            base_url="https://merchant.example",
        ) as client:
            return await client.get("/paid")

    response = asyncio.run(_run())

    assert response.status_code == 200
    assert attempts == 2
    assert captured_signature is not None
    retried_payload = decode_model_from_base64(captured_signature, PaymentPayload)
    assert retried_payload.network == "xrpl:1"

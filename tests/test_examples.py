from __future__ import annotations

import asyncio
from decimal import Decimal
import importlib

import httpx
import pytest
from fastapi import FastAPI
from slowapi import Limiter as SlowLimiter
from xrpl.wallet import Wallet

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.factory import create_app
from xrpl_x402_client import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
)
from xrpl_x402_core import (
    PaymentPayload,
    RLUSD_TESTNET_ISSUER,
    XRPLAmount,
    decode_model_from_base64,
)
from xrpl_x402_facilitator.models import AssetDescriptor, SettleResponse, StructuredAmount, VerifyResponse
from xrpl_x402_middleware import XRPLFacilitatorClient

FACILITATOR_TOKEN = "example-facilitator-token"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rEXAMPLEPAYER123456789"
INVOICE_ID = "EXAMPLE-INVOICE-123"
TX_HASH = "EXAMPLE-TX-HASH-123"


class RecordingFacilitatorService:
    def supported_assets(self) -> list[AssetDescriptor]:
        return [AssetDescriptor(code="XRP", issuer=None)]

    async def verify_payment(
        self,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> VerifyResponse:
        return VerifyResponse(
            valid=True,
            invoice_id=invoice_id or INVOICE_ID,
            amount="0.001 XRP",
            asset=AssetDescriptor(code="XRP", issuer=None),
            amount_details=StructuredAmount(
                value="1000",
                unit="drops",
                asset=AssetDescriptor(code="XRP", issuer=None),
                drops=1000,
            ),
            payer=PAYER,
            destination=DESTINATION,
            message="Payment valid",
        )

    async def settle_payment(
        self,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> SettleResponse:
        return SettleResponse(
            settled=True,
            tx_hash=TX_HASH,
            status="validated",
        )


def _create_app_with_in_memory_rate_limiter(
    *,
    app_settings: Settings,
    xrpl_service: RecordingFacilitatorService,
) -> FastAPI:
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return create_app(
            app_settings=app_settings,
            xrpl_service=xrpl_service,
        )
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


def test_merchant_example_supports_issued_asset_pricing(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("PRICE_ASSET_CODE", "RLUSD")
    monkeypatch.setenv("PRICE_ASSET_ISSUER", "rRLUSDISSUER")
    monkeypatch.setenv("PRICE_ASSET_AMOUNT", "1.25")

    merchant_example = importlib.import_module("examples.merchant_fastapi.app")
    merchant_example = importlib.reload(merchant_example)

    route_config = merchant_example.build_premium_route_config()
    option = route_config.accepts[0]

    assert option.asset.code == "RLUSD"
    assert option.asset.issuer == "rRLUSDISSUER"
    assert option.amount.unit == "issued"
    assert option.amount.value == "1.25"


def test_buyer_example_passes_env_asset_selection(monkeypatch) -> None:
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    signer = buyer_example.XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    captured: dict[str, object] = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    def fake_wrap_httpx_with_xrpl_payment(
        _signer,
        *,
        asset=None,
        transport=None,
        timeout=None,
        **_kwargs,
    ):
        captured["asset"] = asset
        captured["transport"] = transport
        captured["timeout"] = timeout
        return DummyClient()

    monkeypatch.setenv("PAYMENT_ASSET", "RLUSD:rRLUSDISSUER")
    monkeypatch.setattr(
        buyer_example,
        "wrap_httpx_with_xrpl_payment",
        fake_wrap_httpx_with_xrpl_payment,
    )

    response = asyncio.run(
        buyer_example.fetch_paid_resource(
            signer=signer,
            target_url="http://merchant.local/premium",
        )
    )

    assert response.status_code == 200
    assert captured["asset"] == "RLUSD:rRLUSDISSUER"
    assert captured["timeout"] == buyer_example.DEFAULT_REQUEST_TIMEOUT_SECONDS


def test_buyer_example_resolves_testnet_rpc_when_unset(monkeypatch) -> None:
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setattr(
        buyer_example,
        "resolve_testnet_rpc_url",
        lambda: "https://resolved.testnet.rpc/",
    )

    assert buyer_example.rpc_url_from_env() == "https://resolved.testnet.rpc/"


def test_buyer_example_prefers_explicit_rpc_url(monkeypatch) -> None:
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)
    resolver_called = {"value": False}

    monkeypatch.setenv("XRPL_RPC_URL", "https://explicit.testnet.rpc/")
    monkeypatch.setattr(
        buyer_example,
        "resolve_testnet_rpc_url",
        lambda: resolver_called.__setitem__("value", True) or "https://resolved.testnet.rpc/",
    )

    assert buyer_example.rpc_url_from_env() == "https://explicit.testnet.rpc/"
    assert resolver_called["value"] is False


def test_example_quickstart_flow_returns_paid_response(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("PRICE_DROPS", "1000")
    monkeypatch.delenv("PRICE_ASSET_CODE", raising=False)
    monkeypatch.delenv("PRICE_ASSET_ISSUER", raising=False)
    monkeypatch.delenv("PRICE_ASSET_AMOUNT", raising=False)

    merchant_example = importlib.import_module("examples.merchant_fastapi.app")
    merchant_example = importlib.reload(merchant_example)
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    facilitator_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL="https://s.altnet.rippletest.net:51234",
        MY_DESTINATION_ADDRESS=DESTINATION,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        FACILITATOR_BEARER_TOKEN=FACILITATOR_TOKEN,
    )
    facilitator_app = _create_app_with_in_memory_rate_limiter(
        app_settings=facilitator_settings,
        xrpl_service=RecordingFacilitatorService(),
    )
    async_facilitator_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url="http://facilitator.local",
    )
    merchant_app = merchant_example.create_app(
        client_factory=lambda facilitator_url, bearer_token: XRPLFacilitatorClient(
            base_url=facilitator_url,
            bearer_token=bearer_token,
            async_client=async_facilitator_client,
        ),
    )

    signer = buyer_example.XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )

    async def _run() -> httpx.Response:
        response = await buyer_example.fetch_paid_resource(
            signer=signer,
            target_url="http://merchant.local/premium",
            transport=httpx.ASGITransport(app=merchant_app),
        )
        await async_facilitator_client.aclose()
        return response

    response = asyncio.run(_run())

    assert response.status_code == 200
    assert response.json() == {
        "message": "premium content unlocked",
        "payer": PAYER,
        "invoice_id": INVOICE_ID,
        "tx_hash": TX_HASH,
    }


def test_demo_trace_renders_recording_friendly_summary(monkeypatch) -> None:
    demo_trace = importlib.import_module("devtools.demo_trace")
    demo_trace = importlib.reload(demo_trace)

    buyer_wallet = Wallet.create()
    signer = demo_trace.XRPLPaymentSigner(
        buyer_wallet,
        network="xrpl:1",
        autofill_enabled=False,
    )
    destination = DESTINATION
    invoice_id = "A" * 64

    challenge = demo_trace.PaymentRequired(
        error="Payment required",
        accepts=[
            demo_trace.XRPLPaymentOption(
                network="xrpl:1",
                payTo=destination,
                maxAmountRequired="1.25",
                asset=demo_trace.XRPLAsset(code="RLUSD", issuer=RLUSD_TESTNET_ISSUER),
                amount=XRPLAmount(value="1.25", unit="issued"),
                description="premium access",
            )
        ],
    )

    payment_response = demo_trace.PaymentResponse(
        network="xrpl:1",
        payer=buyer_wallet.classic_address,
        payTo=destination,
        invoiceId=invoice_id,
        txHash=TX_HASH,
        settlementStatus="validated",
        asset=demo_trace.XRPLAsset(code="RLUSD", issuer=RLUSD_TESTNET_ISSUER),
        amount=XRPLAmount(value="1.25", unit="issued"),
    )

    balance_snapshots = {
        destination: [2_000_000, 2_000_000],
        buyer_wallet.classic_address: [10_000_000, 9_999_988],
    }
    asset_snapshots = {
        destination: [Decimal("4"), Decimal("5.25")],
        buyer_wallet.classic_address: [Decimal("7"), Decimal("5.75")],
    }
    recorded_signatures: list[str] = []

    def fake_get_validated_balance(_client, address: str) -> int:
        return balance_snapshots[address].pop(0)

    def fake_get_validated_trustline_balance(
        _client,
        address: str,
        issuer: str,
        *,
        currency_code: str = "RLUSD",
    ) -> Decimal:
        assert issuer == RLUSD_TESTNET_ISSUER
        assert currency_code == "RLUSD"
        return asset_snapshots[address].pop(0)

    def handler(request: httpx.Request) -> httpx.Response:
        if PAYMENT_SIGNATURE_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={PAYMENT_REQUIRED_HEADER: demo_trace.encode_model_to_base64(challenge)},
                request=request,
            )

        recorded_signatures.append(request.headers[PAYMENT_SIGNATURE_HEADER])
        return httpx.Response(
            200,
            json={
                "message": "premium content unlocked",
                "payer": buyer_wallet.classic_address,
                "invoice_id": invoice_id,
                "tx_hash": TX_HASH,
            },
            headers={PAYMENT_RESPONSE_HEADER: demo_trace.encode_model_to_base64(payment_response)},
            request=request,
        )

    monkeypatch.setattr(demo_trace, "get_validated_balance", fake_get_validated_balance)
    monkeypatch.setattr(
        demo_trace,
        "get_validated_trustline_balance",
        fake_get_validated_trustline_balance,
    )

    result = asyncio.run(
        demo_trace.run_demo_trace(
            signer=signer,
            rpc_client=object(),
            target_url="http://merchant.local/premium",
            payment_asset=f"RLUSD:{RLUSD_TESTNET_ISSUER}",
            timeout_seconds=1.0,
            invoice_id=invoice_id,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.challenge_status_code == 402
    assert result.final_status_code == 200
    assert result.payment_response is not None
    assert result.payment_response.tx_hash == TX_HASH

    assert recorded_signatures
    payment_payload = decode_model_from_base64(recorded_signatures[0], PaymentPayload)
    assert payment_payload.payload.invoice_id == invoice_id

    output = demo_trace.render_trace(result)
    assert "x402 challenge" in output
    assert "amount: 1.25 RLUSD" in output
    assert f"pay to: {destination}" in output
    assert f"invoice id: {invoice_id}" in output
    assert "XRPL fee: 12 drops (0.000012 XRP)" in output
    assert "Wallet A (merchant/payTo)" in output
    assert "Wallet B (buyer/payer)" in output
    assert "Wallet A: XRP +0.000000, RLUSD +1.25" in output
    assert "Wallet B: XRP -0.000012, RLUSD -1.25" in output
    assert f"tx hash: {TX_HASH}" in output


def test_demo_trace_blocks_unfunded_issued_asset_buyer(monkeypatch) -> None:
    demo_trace = importlib.import_module("devtools.demo_trace")
    demo_trace = importlib.reload(demo_trace)

    buyer_wallet = Wallet.create()
    signer = demo_trace.XRPLPaymentSigner(
        buyer_wallet,
        network="xrpl:1",
        autofill_enabled=False,
    )
    destination = DESTINATION

    challenge = demo_trace.PaymentRequired(
        error="Payment required",
        accepts=[
            demo_trace.XRPLPaymentOption(
                network="xrpl:1",
                payTo=destination,
                maxAmountRequired="1.25",
                asset=demo_trace.XRPLAsset(code="RLUSD", issuer=RLUSD_TESTNET_ISSUER),
                amount=XRPLAmount(value="1.25", unit="issued"),
                description="premium access",
            )
        ],
    )

    xrp_balances = {
        destination: 2_000_000,
        buyer_wallet.classic_address: 10_000_000,
    }
    rlusd_balances = {
        destination: Decimal("30"),
        buyer_wallet.classic_address: Decimal("0"),
    }
    requests = {"paid": 0}
    printed_sections: list[str] = []

    def fake_get_validated_balance(_client, address: str) -> int:
        return xrp_balances[address]

    def fake_get_validated_trustline_balance(
        _client,
        address: str,
        issuer: str,
        *,
        currency_code: str = "RLUSD",
    ) -> Decimal:
        assert issuer == RLUSD_TESTNET_ISSUER
        assert currency_code == "RLUSD"
        return rlusd_balances[address]

    def handler(request: httpx.Request) -> httpx.Response:
        if PAYMENT_SIGNATURE_HEADER in request.headers:
            requests["paid"] += 1
            return httpx.Response(500, request=request)
        return httpx.Response(
            402,
            headers={PAYMENT_REQUIRED_HEADER: demo_trace.encode_model_to_base64(challenge)},
            request=request,
        )

    monkeypatch.setattr(demo_trace, "get_validated_balance", fake_get_validated_balance)
    monkeypatch.setattr(
        demo_trace,
        "get_validated_trustline_balance",
        fake_get_validated_trustline_balance,
    )

    with pytest.raises(
        demo_trace.DemoPreflightError,
        match="Buyer wallet .* only has 0 RLUSD",
    ):
        asyncio.run(
            demo_trace.run_demo_trace(
                signer=signer,
                rpc_client=object(),
                target_url="http://merchant.local/premium",
                payment_asset=f"RLUSD:{RLUSD_TESTNET_ISSUER}",
                timeout_seconds=1.0,
                transport=httpx.MockTransport(handler),
                printer=printed_sections.append,
            )
        )

    assert requests["paid"] == 0
    assert any("Preflight check" in section for section in printed_sections)
    assert any("python -m devtools.rlusd_topup" in section for section in printed_sections)

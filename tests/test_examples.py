from __future__ import annotations

import asyncio
import importlib

import httpx
from fastapi import FastAPI
from slowapi import Limiter as SlowLimiter
from xrpl.wallet import Wallet

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.factory import create_app
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

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
from x402.schemas import PaymentRequirements, SettleResponse
from xrpl.wallet import Wallet


DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


def test_merchant_example_supports_canonical_iou_pricing(monkeypatch) -> None:
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("PRICE_ASSET_CODE", "RLUSD")
    monkeypatch.setenv("PRICE_ASSET_ISSUER", "rRLUSDISSUER")
    monkeypatch.setenv("PRICE_ASSET_AMOUNT", "1.25")

    merchant = importlib.reload(
        importlib.import_module("examples.merchant_fastapi.app")
    )
    option = merchant.build_premium_route_config().accepts

    assert option.price.asset == "RLUSD"
    assert option.price.amount == "1.25"
    assert option.price.extra == {
        "areFeesSponsored": False,
        "assetTransferMethod": "sequence",
        "issuer": "rRLUSDISSUER",
    }


def test_buyer_example_passes_explicit_asset_limit(monkeypatch) -> None:
    buyer = importlib.reload(importlib.import_module("examples.buyer_httpx"))
    signer = buyer.XRPLPaymentSigner(
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
            return httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request("GET", url),
            )

    def fake_wrap(_signer, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return DummyClient()

    monkeypatch.setenv("PAYMENT_ASSET", "RLUSD")
    monkeypatch.setenv("PAYMENT_ASSET_ISSUER", "rRLUSDISSUER")
    monkeypatch.setenv("PAYMENT_MAX_SPEND", "1.25")
    monkeypatch.setattr(buyer, "wrap_httpx_with_xrpl_payment", fake_wrap)

    response = asyncio.run(
        buyer.fetch_paid_resource(
            signer=signer,
            target_url="http://merchant.local/premium",
        )
    )

    limits = captured["asset_limits"]
    assert isinstance(limits, list) and len(limits) == 1
    assert limits[0].asset == "RLUSD"
    assert limits[0].issuer == "rRLUSDISSUER"
    assert limits[0].max_amount == "1.25"
    assert captured["spend_controls"] is None
    assert captured["timeout"] == 30
    assert response.status_code == 200


def test_buyer_example_resolves_testnet_rpc_when_unset(monkeypatch) -> None:
    buyer = importlib.reload(importlib.import_module("examples.buyer_httpx"))
    captured: dict[str, object] = {}
    wallet = Wallet.create()
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed or "")
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setattr(
        buyer,
        "resolve_testnet_rpc_url",
        lambda: "https://resolved.testnet.rpc/",
    )
    monkeypatch.setattr(
        buyer,
        "XRPLPaymentSigner",
        lambda _wallet, **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )

    buyer.build_signer_from_env()
    assert captured["rpc_url"] == "https://resolved.testnet.rpc/"
    assert captured["network"] == "xrpl:1"


def test_buyer_example_prefers_explicit_rpc_url(monkeypatch) -> None:
    buyer = importlib.reload(importlib.import_module("examples.buyer_httpx"))
    captured: dict[str, object] = {}
    wallet = Wallet.create()
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed or "")
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("XRPL_RPC_URL", "https://explicit.testnet.rpc/")
    monkeypatch.setattr(
        buyer,
        "resolve_testnet_rpc_url",
        lambda: (_ for _ in ()).throw(AssertionError("resolver called")),
    )
    monkeypatch.setattr(
        buyer,
        "XRPLPaymentSigner",
        lambda _wallet, **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )

    buyer.build_signer_from_env()
    assert captured["rpc_url"] == "https://explicit.testnet.rpc/"


def test_demo_trace_prints_standard_accepted_and_settlement(monkeypatch, capsys) -> None:
    trace = importlib.reload(importlib.import_module("devtools.demo_trace"))
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    settlement = SettleResponse(
        success=True,
        payer="rPayer",
        transaction="A" * 64,
        network="xrpl:1",
        amount="1000",
    )

    async def fake_pay_with_x402(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["asset"] == "XRP"
        assert kwargs["issuer"] is None
        assert kwargs["max_spend"] == "1000"
        return SimpleNamespace(
            status_code=200,
            paid=True,
            pending=False,
            text='{"message":"premium content unlocked"}',
            accepted=accepted,
            payment_response=settlement,
        )

    monkeypatch.setenv("PAYMENT_ASSET", "XRP")
    monkeypatch.delenv("PAYMENT_ASSET_ISSUER", raising=False)
    monkeypatch.setenv("PAYMENT_MAX_SPEND", "1000")
    monkeypatch.setattr(trace, "pay_with_x402", fake_pay_with_x402)

    asyncio.run(trace.main())
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["paid"] is True
    assert rendered["accepted"]["asset"] == "XRP"
    assert rendered["settlement"]["success"] is True
    assert rendered["settlement"]["transaction"] == "A" * 64

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner
from xrpl.wallet import Wallet

from xrpl_x402_client import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER, XRPLPaymentSigner
from xrpl_x402_core import PaymentRequired, PaymentResponse, XRPLPaymentOption, encode_model_to_base64
from xrpl_x402_payer import ReceiptRecord, create_proxy_app
from xrpl_x402_payer.cli import app
from xrpl_x402_payer.mcp import budget_status as mcp_budget_status
from xrpl_x402_payer.mcp import list_receipts as mcp_list_receipts
from xrpl_x402_payer.mcp import pay_url as mcp_pay_url
from xrpl_x402_payer.mcp import proxy_mode as mcp_proxy_mode
from xrpl_x402_payer.payer import PayResult, XRPLPayer, budget_status
from xrpl_x402_payer.receipts import ReceiptStore

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
RUNNER = CliRunner()


def _payment_required() -> PaymentRequired:
    return PaymentRequired(
        error="Payment required",
        accepts=[
            XRPLPaymentOption(
                network="xrpl:1",
                payTo=DESTINATION,
                maxAmountRequired="1000",
                asset={"code": "XRP"},
                amount={"value": "1000", "unit": "drops", "drops": 1000},
            )
        ],
    )


def _payment_response() -> PaymentResponse:
    return PaymentResponse(
        network="xrpl:1",
        payer="rBuyerAddress123",
        payTo=DESTINATION,
        invoiceId="invoice-123",
        txHash="ABC123",
        settlementStatus="validated",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "drops": 1000},
    )


def _signer() -> XRPLPaymentSigner:
    return XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)


def test_pay_records_receipt_on_success(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)
    challenge = _payment_required()
    payment_response = _payment_response()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                402,
                headers={PAYMENT_REQUIRED_HEADER: encode_model_to_base64(challenge)},
                json=challenge.model_dump(by_alias=True, exclude_none=True),
                request=request,
            )

        assert PAYMENT_SIGNATURE_HEADER in request.headers
        return httpx.Response(
            200,
            headers={PAYMENT_RESPONSE_HEADER: encode_model_to_base64(payment_response)},
            text="paid content",
            request=request,
        )

    result = asyncio.run(
        payer.pay(
            url="https://merchant.example/premium",
            amount=0.001,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.paid is True
    assert result.status_code == 200
    assert result.text == "paid content"
    receipts = store.list(limit=5)
    assert len(receipts) == 1
    assert receipts[0].tx_hash == "ABC123"
    assert attempts == 2


def test_pay_dry_run_handles_plain_402_without_challenge(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="plain 402", request=request)

    result = asyncio.run(
        payer.pay(
            url="https://merchant.example/premium",
            dry_run=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.dry_run is True
    assert result.preview is not None
    assert result.preview["x402_challenge_present"] is False
    assert store.list(limit=5) == []


def test_pay_raises_on_plain_402_without_challenge(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="plain 402", request=request)

    with pytest.raises(ValueError, match="valid x402 challenge"):
        asyncio.run(
            payer.pay(
                url="https://merchant.example/premium",
                transport=httpx.MockTransport(handler),
            )
        )


def test_budget_status_sums_matching_asset(monkeypatch, tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    store.append(
        ReceiptRecord(
            created_at="2025-01-01T00:00:00+00:00",
            url="https://merchant.example/a",
            method="GET",
            status_code=200,
            network="xrpl:1",
            asset_identifier="XRP:native",
            amount="0.001",
            payer="rA",
            tx_hash="A1",
            settlement_status="validated",
        )
    )
    store.append(
        ReceiptRecord(
            created_at="2025-01-01T00:00:01+00:00",
            url="https://merchant.example/b",
            method="GET",
            status_code=200,
            network="xrpl:1",
            asset_identifier="XRP:native",
            amount="0.002",
            payer="rB",
            tx_hash="B1",
            settlement_status="validated",
        )
    )
    store.append(
        ReceiptRecord(
            created_at="2025-01-01T00:00:02+00:00",
            url="https://merchant.example/c",
            method="GET",
            status_code=200,
            network="xrpl:1",
            asset_identifier="RLUSD:rIssuer",
            amount="2",
            payer="rC",
            tx_hash="C1",
            settlement_status="validated",
        )
    )
    monkeypatch.setenv("XRPL_X402_MAX_SPEND", "0.01")

    summary = budget_status(asset="XRP", store=store)

    assert summary["asset_identifier"] == "XRP:native"
    assert summary["spent"] == "0.003"
    assert summary["remaining"] == "0.007"


def test_proxy_app_auto_pays_upstream(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)
    challenge = _payment_required()
    payment_response = _payment_response()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                402,
                headers={PAYMENT_REQUIRED_HEADER: encode_model_to_base64(challenge)},
                json=challenge.model_dump(by_alias=True, exclude_none=True),
                request=request,
            )
        return httpx.Response(
            200,
            headers={PAYMENT_RESPONSE_HEADER: encode_model_to_base64(payment_response)},
            text="proxied body",
            request=request,
        )

    app_under_test = create_proxy_app(
        target_base_url="https://merchant.example",
        payer=payer,
        transport=httpx.MockTransport(handler),
    )

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_under_test),
            base_url="http://proxy.test",
        ) as client:
            return await client.get("/premium")

    response = asyncio.run(_run())

    assert response.status_code == 200
    assert response.text == "proxied body"
    assert store.list(limit=1)[0].tx_hash == "ABC123"


def test_proxy_app_dry_run_does_not_require_wallet_seed(monkeypatch) -> None:
    monkeypatch.delenv("XRPL_WALLET_SEED", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="plain 402", request=request)

    app_under_test = create_proxy_app(
        target_base_url="https://merchant.example",
        dry_run=True,
        transport=httpx.MockTransport(handler),
    )

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_under_test),
            base_url="http://proxy.test",
        ) as client:
            return await client.get("/premium")

    response = asyncio.run(_run())

    assert response.status_code == 402
    assert response.text == "plain 402"


def test_cli_skill_install_copies_bundled_skill(tmp_path) -> None:
    destination = tmp_path / "skills" / "xrpl-x402-payer"

    result = RUNNER.invoke(app, ["skill", "install", "--destination", str(destination)])

    assert result.exit_code == 0
    assert (destination / "SKILL.md").exists()


def test_cli_mcp_command_lazy_imports_module(monkeypatch) -> None:
    called = {"count": 0}

    def fake_main() -> None:
        called["count"] += 1

    module = SimpleNamespace(main=fake_main)
    monkeypatch.setitem(sys.modules, "xrpl_x402_payer.mcp", module)

    result = RUNNER.invoke(app, ["mcp"])

    assert result.exit_code == 0
    assert called["count"] == 1


def test_cli_pay_uses_shared_service(monkeypatch) -> None:
    async def fake_pay_with_x402(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"ok",
            headers={},
            challenge_present=False,
            dry_run=False,
            paid=False,
        )

    monkeypatch.setattr("xrpl_x402_payer.cli.pay_with_x402", fake_pay_with_x402)

    result = RUNNER.invoke(app, ["pay", "https://merchant.example/premium"])

    assert result.exit_code == 0
    assert "ok" in result.output


def test_mcp_helpers_format_outputs(monkeypatch) -> None:
    async def fake_pay_with_x402(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"mcp body",
            headers={},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    monkeypatch.setattr("xrpl_x402_payer.mcp.pay_with_x402", fake_pay_with_x402)
    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.get_receipts",
        lambda limit=10: [
            {
                "url": "https://merchant.example/premium",
                "amount": "0.001",
                "asset_identifier": "XRP:native",
                "tx_hash": "ABC123",
            }
        ],
    )
    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.get_budget_status",
        lambda asset="XRP", issuer=None: {"asset_identifier": "XRP:native", "spent": "0.001"},
    )
    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.proxy_manager",
        SimpleNamespace(start=lambda **_: "http://127.0.0.1:8787"),
    )

    assert asyncio.run(mcp_pay_url("https://merchant.example/premium")) == "mcp body"
    assert "ABC123" in asyncio.run(mcp_list_receipts())
    assert json.loads(asyncio.run(mcp_budget_status()))["spent"] == "0.001"
    assert "127.0.0.1:8787" in asyncio.run(mcp_proxy_mode("https://merchant.example"))

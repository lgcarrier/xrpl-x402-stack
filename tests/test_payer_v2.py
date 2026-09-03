from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner
from x402.extensions.payment_identifier import (
    declare_payment_identifier_extension,
    extract_payment_identifier,
)
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    encode_payment_required_header,
    encode_payment_response_header,
)
from x402.schemas import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
)
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLAssetSpendLimit, XRPLPaymentSigner
from xrpl_x402_payer import ReceiptRecord, XRPLPayer, create_proxy_app
from xrpl_x402_payer.cli import app as cli_app
from xrpl_x402_payer.mcp import (
    budget_status as mcp_budget_status,
    build_xrpl_payment_client,
    list_receipts as mcp_list_receipts,
    pay_url as mcp_pay_url,
    proxy_mode as mcp_proxy_mode,
    wrap_mcp_client_with_xrpl_payment,
)
from xrpl_x402_payer.payer import (
    DEVNET_RPC_URL,
    MAINNET_RPC_URL,
    PayResult,
    _request_fingerprint,
    budget_status,
    build_signer_from_env,
)
from xrpl_x402_payer.receipts import ReceiptStore
from xrpl_x402_core import RLUSD_HEX, RLUSD_TESTNET_ISSUER

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
RUNNER = CliRunner()


def challenge(
    *,
    amount: str = "1000",
    resource_url: str = "https://merchant.example/paid",
    require_payment_identifier: bool = False,
) -> PaymentRequired:
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount=amount,
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    return PaymentRequired(
        resource=ResourceInfo(
            url=resource_url,
            description="Paid endpoint",
            mime_type="application/json",
        ),
        accepts=[accepted],
        extensions=(
            {
                "payment-identifier": declare_payment_identifier_extension(
                    required=True
                )
            }
            if require_payment_identifier
            else None
        ),
    )


def offline_signer() -> XRPLPaymentSigner:
    return XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=1014,
    )


def test_payer_uses_one_automatic_retry_and_records_standard_receipt(tmp_path) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        challenge()
                    )
                },
                request=request,
            )
        assert request.headers.get(PAYMENT_SIGNATURE_HEADER)
        return httpx.Response(
            200,
            json={"secret": True},
            headers={
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(
                    SettleResponse(
                        success=True,
                        payer="rPayer",
                        transaction="C" * 64,
                        network="xrpl:1",
                        amount="1000",
                    )
                )
            },
            request=request,
        )

    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(offline_signer(), store=store)
    result = asyncio.run(
        payer.pay(
            url="https://merchant.example/paid",
            asset="XRP",
            max_spend="1000",
            transport=httpx.MockTransport(handler),
        )
    )

    assert len(calls) == 2
    assert result.paid is True
    assert result.pending is False
    assert result.accepted == challenge().accepts[0]
    saved = store.list()[0]
    assert isinstance(saved, ReceiptRecord)
    assert saved.state == "paid"
    assert saved.settlement.transaction == "C" * 64
    assert saved.accepted.amount == "1000"


def test_payer_rejects_legacy_body_challenge() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            402,
            json={
                "x402Version": 1,
                "accepts": [],
                "error": "legacy",
            },
            request=request,
        )

    with pytest.raises(Exception, match="canonical x402 v2 PAYMENT-REQUIRED"):
        asyncio.run(
            XRPLPayer(offline_signer()).pay(
                url="https://merchant.example/paid",
                asset="XRP",
                max_spend="1000",
                transport=httpx.MockTransport(handler),
            )
        )
    assert calls == 1


def test_payer_does_not_accept_legacy_settlement_header(tmp_path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        challenge()
                    )
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"secret": True},
            headers={
                "X-PAYMENT-RESPONSE": encode_payment_response_header(
                    SettleResponse(
                        success=True,
                        payer="rPayer",
                        transaction="E" * 64,
                        network="xrpl:1",
                        amount="1000",
                    )
                )
            },
            request=request,
        )

    store = ReceiptStore(tmp_path / "legacy.jsonl")
    result = asyncio.run(
        XRPLPayer(offline_signer(), store=store).pay(
            url="https://merchant.example/paid",
            asset="XRP",
            max_spend="1000",
            transport=httpx.MockTransport(handler),
        )
    )
    assert calls == 2
    assert result.paid is False
    assert result.payment_response.error_reason == "settlement_pending"
    assert result.payment_response.transaction
    assert result.body == b""
    assert store.list()[0].state == "pending"


def test_pending_receipt_is_never_marked_paid(tmp_path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        challenge()
                    )
                },
                request=request,
            )
        return httpx.Response(
            402,
            headers={
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(
                    SettleResponse(
                        success=False,
                        error_reason="settlement_pending",
                        transaction="D" * 64,
                        network="xrpl:1",
                        amount="1000",
                    )
                )
            },
            request=request,
        )

    store = ReceiptStore(tmp_path / "pending.jsonl")
    result = asyncio.run(
        XRPLPayer(offline_signer(), store=store).pay(
            url="https://merchant.example/paid",
            asset="XRP",
            max_spend="1000",
            transport=httpx.MockTransport(handler),
        )
    )
    assert result.paid is False
    assert result.pending is True
    assert store.list()[0].state == "pending"


def test_pending_http_retry_reuses_identical_payload_without_resigning(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []
    paid_headers: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        signature = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        if signature is None:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        challenge()
                    )
                },
                request=request,
            )

        paid_headers.append(signature)
        settlement = (
            SettleResponse(
                success=False,
                error_reason="settlement_pending",
                transaction="D" * 64,
                network="xrpl:1",
                amount="1000",
            )
            if len(paid_headers) == 1
            else SettleResponse(
                success=True,
                payer="rPayer",
                transaction="D" * 64,
                network="xrpl:1",
                amount="1000",
            )
        )
        return httpx.Response(
            402 if not settlement.success else 200,
            headers={
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(
                    settlement
                )
            },
            request=request,
        )

    signer = offline_signer()
    store = ReceiptStore(tmp_path / "pending-resume.jsonl")
    transport = httpx.MockTransport(handler)
    first = asyncio.run(
        XRPLPayer(signer, store=store).pay(
            url="https://merchant.example/paid",
            method="POST",
            content=b'{"operation":"once"}',
            asset="XRP",
            max_spend="1000",
            transport=transport,
        )
    )
    assert first.pending is True
    assert first.receipt is not None
    attempt = next(iter(json.loads(store.index_path.read_text())["attempts"].values()))
    assert attempt["payment_payload"]["payload"]["signedTxBlob"]

    second = asyncio.run(
        XRPLPayer(None, payer_account=signer.classic_address, store=store).pay(
            url="https://merchant.example/paid",
            method="POST",
            content=b'{"operation":"once"}',
            asset="XRP",
            max_spend="1000",
            transport=transport,
        )
    )

    assert second.paid is True
    assert len(requests) == 4
    assert paid_headers[0] == paid_headers[1]
    records = store.list()
    assert records[0].state == "paid"
    assert all("paymentPayload" not in record.model_dump(by_alias=True) for record in records)


def test_direct_mcp_client_records_and_resumes_pending_payment(tmp_path) -> None:
    class FakeMCPClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.paid_payloads: list[dict[str, object]] = []

        async def call_tool(self, params, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            self.calls.append(params)
            payment = params.get("_meta", {}).get("x402/payment")
            if payment is None:
                return SimpleNamespace(
                    content=[],
                    isError=True,
                    structuredContent=challenge().model_dump(
                        by_alias=True, exclude_none=True
                    ),
                    _meta={},
                )

            self.paid_payloads.append(payment)
            settlement = (
                SettleResponse(
                    success=False,
                    error_reason="settlement_pending",
                    transaction="9" * 64,
                    network="xrpl:1",
                    amount="1000",
                )
                if len(self.paid_payloads) == 1
                else SettleResponse(
                    success=True,
                    payer="rPayer",
                    transaction="9" * 64,
                    network="xrpl:1",
                    amount="1000",
                )
            )
            return SimpleNamespace(
                content=[{"type": "text", "text": "paid"}],
                isError=not settlement.success,
                structuredContent=None,
                _meta={
                    "x402/payment-response": settlement.model_dump(
                        by_alias=True, exclude_none=True
                    )
                },
            )

    raw_client = FakeMCPClient()
    store = ReceiptStore(tmp_path / "mcp-pending.jsonl")
    client = wrap_mcp_client_with_xrpl_payment(
        raw_client,
        offline_signer(),
        asset_limits=[
            XRPLAssetSpendLimit(
                network="xrpl:1",
                asset="XRP",
                max_amount="1000",
            )
        ],
        store=store,
        recovery_scope="https://mcp.example/sse|principal:test-user",
    )

    first = asyncio.run(client.call_tool("write_once", {"value": 7}))
    second = asyncio.run(client.call_tool("write_once", {"value": 7}))

    assert first.payment_response is not None
    assert first.payment_response.error_reason == "settlement_pending"
    assert second.payment_response is not None
    assert second.payment_response.success is True
    assert len(raw_client.calls) == 4
    assert "_meta" not in raw_client.calls[0]
    assert "_meta" not in raw_client.calls[2]
    assert raw_client.paid_payloads[0] == raw_client.paid_payloads[1]
    records = store.list()
    assert [record.state for record in records[:2]] == ["paid", "pending"]
    assert all("paymentPayload" not in record.model_dump(by_alias=True) for record in records)
    assert all(record.method == "MCP:write_once" for record in records[:2])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"asset": "XRP"},
        {"asset": "USDC", "max_spend": "1"},
        {"asset": None, "max_spend": "1"},
    ],
)
def test_explicit_assets_require_issuer_and_cap(kwargs) -> None:  # type: ignore[no-untyped-def]
    payer = XRPLPayer(None)
    with pytest.raises(ValueError):
        asyncio.run(
            payer.pay(
                url="https://merchant.example/paid",
                dry_run=True,
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, request=request)
                ),
                **kwargs,
            )
        )


def test_mcp_client_uses_the_same_xrpl_spend_controls() -> None:
    client = build_xrpl_payment_client(
        offline_signer(),
        asset_limits=[
            XRPLAssetSpendLimit(
                network="xrpl:1",
                asset="XRP",
                max_amount="1000",
            )
        ],
    )
    assert client.get_registered_schemes()[2] == [
        {"network": "xrpl:1", "scheme": "exact"}
    ]


def test_default_pegged_asset_policy_is_issuer_aware() -> None:
    client = build_xrpl_payment_client(offline_signer())
    malicious = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": Wallet.create().classic_address,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    challenge = PaymentRequired(
        resource=ResourceInfo(url="https://merchant.example/paid"),
        accepts=[malicious],
    )
    with pytest.raises(Exception, match="filtered out by policies"):
        asyncio.run(client.create_payment_payload(challenge))


def test_default_pegged_asset_cap_treats_integer_iou_as_ledger_value() -> None:
    client = build_xrpl_payment_client(offline_signer())
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="2",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    challenge = PaymentRequired(
        resource=ResourceInfo(url="https://merchant.example/paid"),
        accepts=[accepted],
    )
    with pytest.raises(Exception, match="max_amount_per_payment"):
        asyncio.run(client.create_payment_payload(challenge))


def test_budget_normalizes_default_iou_currency_codes(tmp_path) -> None:
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    store = ReceiptStore(tmp_path / "rlusd.jsonl")
    store.append(
        ReceiptRecord(
            created_at="2026-08-30T00:00:00+00:00",
            url="https://merchant.example/paid",
            method="GET",
            status_code=200,
            state="paid",
            settlement=SettleResponse(
                success=True,
                payer="rPayer",
                transaction="E" * 64,
                network="xrpl:1",
                amount="0.5",
            ),
            accepted=accepted,
        )
    )
    summary = store.budget_summary(
        network="xrpl:1",
        asset="RLUSD",
        issuer=RLUSD_TESTNET_ISSUER,
    )
    assert summary["spent"] == "0.5"


def test_dry_run_decodes_canonical_challenge_without_signing_or_retrying(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            402,
            headers={
                PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                    challenge()
                )
            },
            request=request,
        )

    store = ReceiptStore(tmp_path / "dry-run.jsonl")
    result = asyncio.run(
        XRPLPayer(None, store=store).pay(
            url="https://merchant.example/paid",
            asset="XRP",
            max_spend="999",
            dry_run=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert len(requests) == 1
    assert PAYMENT_SIGNATURE_HEADER not in requests[0].headers
    assert result.challenge_present is True
    assert result.preview is not None
    assert result.preview["challengePresent"] is True
    assert result.preview["wouldPay"] is False
    assert result.preview["accepted"] == challenge().accepts[0].model_dump(
        by_alias=True, exclude_none=True
    )
    assert store.list() == []


def test_dry_run_tolerates_plain_402_without_legacy_body_fallback(
    tmp_path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={"x402Version": 1, "accepts": []},
            request=request,
        )

    store = ReceiptStore(tmp_path / "plain-402.jsonl")
    result = asyncio.run(
        XRPLPayer(None, store=store).pay(
            url="https://merchant.example/paid",
            dry_run=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.status_code == 402
    assert result.challenge_present is False
    assert result.preview == {
        "mode": "dry_run",
        "x402Version": 2,
        "challengePresent": False,
    }
    assert store.list() == []


@pytest.mark.parametrize(
    ("network", "rpc_url", "expected_rpc"),
    [
        ("xrpl:0", None, MAINNET_RPC_URL),
        ("xrpl:1", None, "https://resolved.testnet.rpc/"),
        ("xrpl:2", None, DEVNET_RPC_URL),
        ("xrpl:21337", "https://custom.rpc/", "https://custom.rpc/"),
    ],
)
def test_build_signer_routes_every_supported_network_family(
    monkeypatch,
    network: str,
    rpc_url: str | None,
    expected_rpc: str,
) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    captured: dict[str, object] = {}
    resolver_calls = 0

    class FakeSigner:
        def __init__(self, wallet_arg, *, rpc_url: str, network: str) -> None:
            captured["wallet"] = wallet_arg
            captured["rpc_url"] = rpc_url
            captured["network"] = network

    def resolve_testnet() -> str:
        nonlocal resolver_calls
        resolver_calls += 1
        return "https://resolved.testnet.rpc/"

    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.delenv("XRPL_NETWORK", raising=False)
    monkeypatch.delenv("NETWORK_ID", raising=False)
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setattr("xrpl_x402_payer.payer.XRPLPaymentSigner", FakeSigner)
    monkeypatch.setattr(
        "xrpl_x402_payer.payer.resolve_testnet_rpc_url", resolve_testnet
    )

    build_signer_from_env(network=network, rpc_url=rpc_url)

    assert captured["wallet"].classic_address == wallet.classic_address
    assert captured["rpc_url"] == expected_rpc
    assert captured["network"] == network
    assert resolver_calls == (1 if network == "xrpl:1" else 0)


def test_build_signer_requires_rpc_for_custom_network(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)

    with pytest.raises(
        RuntimeError, match="XRPL_RPC_URL is required for custom XRPL networks"
    ):
        build_signer_from_env(network="xrpl:21337")


def test_proxy_replays_canonical_payment_and_preserves_request_semantics(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "https://merchant.example/api/paid?view=full"
        assert request.method == "POST"
        assert request.content == b'{"request":true}'
        assert request.headers["x-caller"] == "proxy-test"
        assert request.headers.get("connection") != "close"
        if len(requests) == 1:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        challenge()
                    )
                },
                request=request,
            )
        assert request.headers.get(PAYMENT_SIGNATURE_HEADER)
        return httpx.Response(
            200,
            content=b"proxied body",
            headers={
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(
                    SettleResponse(
                        success=True,
                        payer="rPayer",
                        transaction="F" * 64,
                        network="xrpl:1",
                        amount="1000",
                    )
                ),
                "connection": "close",
            },
            request=request,
        )

    store = ReceiptStore(tmp_path / "proxy.jsonl")
    app = create_proxy_app(
        target_base_url="https://merchant.example/api",
        asset="XRP",
        max_spend="1000",
        payer=XRPLPayer(offline_signer(), store=store),
        transport=httpx.MockTransport(handler),
    )

    async def run_request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            return await client.post(
                "/paid?view=full",
                headers={"x-caller": "proxy-test", "connection": "close"},
                content=b'{"request":true}',
            )

    response = asyncio.run(run_request())

    assert len(requests) == 2
    assert response.status_code == 200
    assert response.content == b"proxied body"
    assert response.headers.get(PAYMENT_RESPONSE_HEADER)
    assert response.headers.get("connection") is None
    assert store.list()[0].state == "paid"


def test_receipt_budget_isolates_network_issuer_and_terminal_state(
    tmp_path,
) -> None:
    other_issuer = Wallet.create().classic_address
    store = ReceiptStore(tmp_path / "isolated-budget.jsonl")

    def append_receipt(
        *,
        network: str,
        issuer: str,
        amount: str,
        state: str = "paid",
    ) -> None:
        success = state == "paid"
        accepted = PaymentRequirements(
            scheme="exact",
            network=network,
            asset=RLUSD_HEX,
            amount=amount,
            pay_to=DESTINATION,
            max_timeout_seconds=60,
            extra={
                "issuer": issuer,
                "areFeesSponsored": False,
                "assetTransferMethod": "sequence",
            },
        )
        store.append(
            ReceiptRecord(
                created_at="2026-08-30T00:00:00+00:00",
                url="https://merchant.example/paid",
                method="GET",
                status_code=200 if success else 402,
                state=state,
                settlement=SettleResponse(
                    success=success,
                    payer="rPayer" if success else None,
                    error_reason=None if success else "settlement_pending",
                    transaction="A" * 64,
                    network=network,
                    amount=amount,
                ),
                accepted=accepted,
            )
        )

    append_receipt(
        network="xrpl:1", issuer=RLUSD_TESTNET_ISSUER, amount="0.5"
    )
    append_receipt(
        network="xrpl:0", issuer=RLUSD_TESTNET_ISSUER, amount="1"
    )
    append_receipt(network="xrpl:1", issuer=other_issuer, amount="2")
    append_receipt(
        network="xrpl:1",
        issuer=RLUSD_TESTNET_ISSUER,
        amount="8",
        state="pending",
    )

    selected = budget_status(
        asset="RLUSD",
        issuer=RLUSD_TESTNET_ISSUER,
        network="xrpl:1",
        max_spend="1",
        store=store,
    )
    mainnet = budget_status(
        asset="RLUSD",
        issuer=RLUSD_TESTNET_ISSUER,
        network="xrpl:0",
        store=store,
    )
    other = budget_status(
        asset="RLUSD",
        issuer=other_issuer,
        network="xrpl:1",
        store=store,
    )

    assert selected["spent"] == "0.5"
    assert selected["remaining"] == "0.5"
    assert mainnet["spent"] == "1"
    assert other["spent"] == "2"


def test_explicit_iou_controls_bind_network_issuer_and_decimal_cap() -> None:
    allowed_issuer = Wallet.create().classic_address
    other_issuer = Wallet.create().classic_address
    client = build_xrpl_payment_client(
        offline_signer(),
        asset_limits=[
            XRPLAssetSpendLimit(
                network="xrpl:1",
                asset="USD",
                issuer=allowed_issuer,
                max_amount="0.5",
            )
        ],
    )

    def payment_required(*, issuer: str, amount: str) -> PaymentRequired:
        return PaymentRequired(
            resource=ResourceInfo(url="https://merchant.example/paid"),
            accepts=[
                PaymentRequirements(
                    scheme="exact",
                    network="xrpl:1",
                    asset="USD",
                    amount=amount,
                    pay_to=DESTINATION,
                    max_timeout_seconds=60,
                    extra={
                        "issuer": issuer,
                        "areFeesSponsored": False,
                        "assetTransferMethod": "sequence",
                    },
                )
            ],
        )

    accepted = asyncio.run(
        client.create_payment_payload(
            payment_required(issuer=allowed_issuer, amount="0.5")
        )
    )
    assert accepted.accepted.amount == "0.5"
    assert accepted.accepted.extra["issuer"] == allowed_issuer

    for disallowed in (
        payment_required(issuer=other_issuer, amount="0.5"),
        payment_required(issuer=allowed_issuer, amount="0.500001"),
    ):
        with pytest.raises(Exception, match="filtered out by policies"):
            asyncio.run(client.create_payment_payload(disallowed))


def test_cli_pay_and_mcp_command_keep_shared_v2_wiring(monkeypatch) -> None:
    pay_kwargs: dict[str, object] = {}

    async def fake_pay_with_x402(**kwargs: object) -> PayResult:
        pay_kwargs.update(kwargs)
        return PayResult(
            status_code=402,
            body=b"dry-run preview",
            headers={},
            challenge_present=True,
            dry_run=True,
            paid=False,
        )

    monkeypatch.setattr(
        "xrpl_x402_payer.cli.pay_with_x402", fake_pay_with_x402
    )
    pay_result = RUNNER.invoke(
        cli_app,
        [
            "pay",
            "https://merchant.example/paid",
            "--asset",
            "XRP",
            "--max-spend",
            "1000",
            "--dry-run",
        ],
    )

    assert pay_result.exit_code == 0
    assert "dry-run preview" in pay_result.output
    assert pay_kwargs == {
        "url": "https://merchant.example/paid",
        "asset": "XRP",
        "issuer": None,
        "max_spend": "1000",
        "dry_run": True,
    }

    called = 0

    def fake_main() -> None:
        nonlocal called
        called += 1

    monkeypatch.setitem(
        sys.modules, "xrpl_x402_payer.mcp", SimpleNamespace(main=fake_main)
    )
    mcp_result = RUNNER.invoke(cli_app, ["mcp"])
    assert mcp_result.exit_code == 0
    assert called == 1


def test_mcp_helpers_forward_spend_controls_and_format_canonical_records(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_pay_with_x402(**kwargs: object) -> PayResult:
        captured["pay"] = kwargs
        return PayResult(
            status_code=200,
            body=b"mcp paid body",
            headers={},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.pay_with_x402", fake_pay_with_x402
    )
    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.get_receipts",
        lambda limit=10: [
            {
                "state": "paid",
                "accepted": {
                    "network": "xrpl:1",
                    "asset": "XRP",
                    "amount": "1000",
                },
                "settlement": {
                    "success": True,
                    "transaction": "A" * 64,
                },
            }
        ],
    )

    def fake_budget_status(**kwargs: object) -> dict[str, object]:
        captured["budget"] = kwargs
        return {"network": "xrpl:1", "asset": "XRP", "spent": "1000"}

    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.get_budget_status", fake_budget_status
    )

    class FakeProxyManager:
        def start(self, **kwargs: object) -> str:
            captured["proxy"] = kwargs
            return "http://127.0.0.1:8787"

    monkeypatch.setattr(
        "xrpl_x402_payer.mcp.proxy_manager", FakeProxyManager()
    )

    paid = asyncio.run(
        mcp_pay_url(
            "https://merchant.example/paid",
            asset="XRP",
            max_spend="1000",
            dry_run=True,
        )
    )
    receipts = json.loads(asyncio.run(mcp_list_receipts(limit=1)))
    summary = json.loads(
        asyncio.run(
            mcp_budget_status(asset="XRP", issuer=None, max_spend="2000")
        )
    )
    proxy = asyncio.run(
        mcp_proxy_mode(
            "https://merchant.example",
            local_port=8790,
            asset="XRP",
            max_spend="1000",
            dry_run=True,
        )
    )

    assert paid == "mcp paid body"
    assert captured["pay"] == {
        "url": "https://merchant.example/paid",
        "asset": "XRP",
        "issuer": None,
        "max_spend": "1000",
        "dry_run": True,
    }
    assert receipts[0]["accepted"]["network"] == "xrpl:1"
    assert summary["spent"] == "1000"
    assert captured["budget"] == {
        "asset": "XRP",
        "issuer": None,
        "max_spend": "2000",
    }
    assert "127.0.0.1:8787" in proxy
    assert captured["proxy"] == {
        "target_base_url": "https://merchant.example",
        "port": 8790,
        "asset": "XRP",
        "issuer": None,
        "max_spend": "1000",
        "dry_run": True,
    }


def test_http_attempt_survives_lost_paid_response_and_reuses_exact_payload(
    tmp_path,
) -> None:
    signer = offline_signer()
    required = challenge(require_payment_identifier=True)
    paid_headers: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        signature = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        if signature is None:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        required
                    )
                },
                request=request,
            )
        persisted = json.loads(store.index_path.read_text())["attempts"]
        assert len(persisted) == 1
        assert next(iter(persisted.values()))["payment_payload"]
        assert next(iter(persisted.values()))["payment_identifier"]
        paid_headers.append(signature)
        if len(paid_headers) == 1:
            raise httpx.ReadTimeout("response was lost", request=request)
        return httpx.Response(
            200,
            headers={
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(
                    SettleResponse(
                        success=True,
                        payer=signer.classic_address,
                        transaction="8" * 64,
                        network="xrpl:1",
                        amount="1000",
                    )
                )
            },
            request=request,
        )

    path = tmp_path / "http-crash.jsonl"
    store = ReceiptStore(path)
    request_kwargs = {
        "url": "https://merchant.example/paid",
        "method": "POST",
        "headers": {"authorization": "Bearer never-persist-this"},
        "content": b'{"write":"once"}',
        "asset": "XRP",
        "max_spend": "1000",
        "transport": httpx.MockTransport(handler),
    }
    with pytest.raises(httpx.ReadTimeout, match="response was lost"):
        asyncio.run(XRPLPayer(signer, store=store).pay(**request_kwargs))

    raw_index = store.index_path.read_text()
    assert "never-persist-this" not in raw_index
    attempt_data = next(iter(json.loads(raw_index)["attempts"].values()))
    attempt_payload = PaymentPayload.model_validate(
        attempt_data["payment_payload"]
    )
    assert attempt_data["payment_identifier"] == extract_payment_identifier(
        attempt_payload
    )
    assert attempt_data["payment_identifier"]
    assert len(paid_headers) == 1
    pending = store.list()[0]
    assert pending.state == "pending"
    assert pending.status_code == 0
    assert pending.settlement.transaction

    recovered = asyncio.run(
        XRPLPayer(
            None,
            payer_account=signer.classic_address,
            store=ReceiptStore(path),
        ).pay(**request_kwargs)
    )
    assert recovered.paid is True
    assert paid_headers[0] == paid_headers[1]
    assert [record.state for record in store.list()] == ["paid", "pending"]


def test_concurrent_http_payers_share_one_signed_attempt_and_receipt(
    tmp_path,
) -> None:
    signer = offline_signer()
    required = challenge(require_payment_identifier=True)
    paid_headers: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        signature = request.headers.get(PAYMENT_SIGNATURE_HEADER)
        if signature is None:
            return httpx.Response(
                402,
                headers={
                    PAYMENT_REQUIRED_HEADER: encode_payment_required_header(
                        required
                    )
                },
                request=request,
            )
        paid_headers.append(signature)
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            headers={
                PAYMENT_RESPONSE_HEADER: encode_payment_response_header(
                    SettleResponse(
                        success=True,
                        payer=signer.classic_address,
                        transaction="7" * 64,
                        network="xrpl:1",
                        amount="1000",
                    )
                )
            },
            request=request,
        )

    path = tmp_path / "http-concurrent.jsonl"

    async def run() -> list[PayResult]:
        common = {
            "url": "https://merchant.example/paid",
            "method": "POST",
            "headers": {"x-principal": "user-42"},
            "content": b'{"operation":"one"}',
            "asset": "XRP",
            "max_spend": "1000",
        }
        return list(
            await asyncio.gather(
                XRPLPayer(signer, store=ReceiptStore(path)).pay(
                    **common, transport=httpx.MockTransport(handler)
                ),
                XRPLPayer(signer, store=ReceiptStore(path)).pay(
                    **common, transport=httpx.MockTransport(handler)
                ),
            )
        )

    results = asyncio.run(run())
    assert all(result.paid for result in results)
    assert len(paid_headers) == 2
    assert paid_headers[0] == paid_headers[1]
    records = ReceiptStore(path).list()
    assert len(records) == 1
    assert records[0].state == "paid"


def test_http_fingerprint_binds_payer_headers_body_and_request_target() -> None:
    common = {
        "url": "https://merchant.example/paid?view=full",
        "method": "POST",
        "headers": {"authorization": "Bearer principal-a"},
        "content": b'{"value":1}',
    }
    baseline = _request_fingerprint(payer="rPayerA", **common)
    assert baseline != _request_fingerprint(payer="rPayerB", **common)
    assert baseline != _request_fingerprint(
        payer="rPayerA", **{**common, "method": "PUT"}
    )
    assert baseline != _request_fingerprint(
        payer="rPayerA", **{**common, "content": b'{"value":2}'}
    )
    assert baseline != _request_fingerprint(
        payer="rPayerA",
        **{**common, "headers": {"authorization": "Bearer principal-b"}},
    )
    assert baseline != _request_fingerprint(
        payer="rPayerA",
        **{**common, "url": "https://merchant.example/other"},
    )


def test_concurrent_mcp_clients_share_one_signed_attempt_and_scope_is_hashed(
    tmp_path,
) -> None:
    class FakeMCPClient:
        def __init__(self) -> None:
            self.paid_payloads: list[dict[str, object]] = []

        async def call_tool(self, params, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            payment = params.get("_meta", {}).get("x402/payment")
            if payment is None:
                return SimpleNamespace(
                    content=[],
                    isError=True,
                    structuredContent=challenge(
                        require_payment_identifier=True
                    ).model_dump(by_alias=True, exclude_none=True),
                    _meta={},
                )
            persisted = json.loads(path.with_suffix(".jsonl.attempts.json").read_text())
            assert len(persisted["attempts"]) == 1
            assert next(iter(persisted["attempts"].values()))["payment_payload"]
            self.paid_payloads.append(payment)
            await asyncio.sleep(0.02)
            settlement = SettleResponse(
                success=True,
                payer="rPayer",
                transaction="6" * 64,
                network="xrpl:1",
                amount="1000",
            )
            return SimpleNamespace(
                content=[],
                isError=False,
                structuredContent=None,
                _meta={
                    "x402/payment-response": settlement.model_dump(
                        by_alias=True, exclude_none=True
                    )
                },
            )

    signer = offline_signer()
    raw_client = FakeMCPClient()
    path = tmp_path / "mcp-concurrent.jsonl"
    scope = "https://mcp.example/sse|principal:user-42"
    limit = [
        XRPLAssetSpendLimit(
            network="xrpl:1", asset="XRP", max_amount="1000"
        )
    ]
    clients = [
        wrap_mcp_client_with_xrpl_payment(
            raw_client,
            signer,
            asset_limits=limit,
            store=ReceiptStore(path),
            recovery_scope=scope,
        )
        for _ in range(2)
    ]

    async def run() -> list[object]:
        return list(
            await asyncio.gather(
                *(client.call_tool("write_once", {"value": 9}) for client in clients)
            )
        )

    results = asyncio.run(run())
    assert all(result.payment_response.success for result in results)
    assert len(raw_client.paid_payloads) == 2
    assert raw_client.paid_payloads[0] == raw_client.paid_payloads[1]
    assert len(ReceiptStore(path).list()) == 1
    assert scope not in ReceiptStore(path).index_path.read_text()


def test_mcp_crash_recovery_reuses_payload_and_rejects_changed_challenge(
    tmp_path,
) -> None:
    class FakeMCPClient:
        def __init__(self) -> None:
            self.free_calls = 0
            self.paid_payloads: list[dict[str, object]] = []
            self.fail_first_paid = True

        async def call_tool(self, params, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            payment = params.get("_meta", {}).get("x402/payment")
            if payment is None:
                self.free_calls += 1
                amount = "1000" if self.free_calls < 3 else "2000"
                return SimpleNamespace(
                    content=[],
                    isError=True,
                    structuredContent=challenge(
                        amount=amount, require_payment_identifier=True
                    ).model_dump(by_alias=True, exclude_none=True),
                    _meta={},
                )
            self.paid_payloads.append(payment)
            if self.fail_first_paid:
                self.fail_first_paid = False
                raise ConnectionError("MCP response was lost")
            settlement = SettleResponse(
                success=False,
                error_reason="settlement_pending",
                transaction="5" * 64,
                network="xrpl:1",
                amount="1000",
            )
            return SimpleNamespace(
                content=[],
                isError=True,
                structuredContent=None,
                _meta={
                    "x402/payment-response": settlement.model_dump(
                        by_alias=True, exclude_none=True
                    )
                },
            )

    signer = offline_signer()
    raw_client = FakeMCPClient()
    path = tmp_path / "mcp-crash.jsonl"
    client = wrap_mcp_client_with_xrpl_payment(
        raw_client,
        signer,
        asset_limits=[
            XRPLAssetSpendLimit(
                network="xrpl:1", asset="XRP", max_amount="1000"
            )
        ],
        store=ReceiptStore(path),
        recovery_scope="https://mcp.example/sse|principal:user-42",
    )

    with pytest.raises(ConnectionError, match="response was lost"):
        asyncio.run(client.call_tool("write_once", {"value": 11}))
    recovered = asyncio.run(client.call_tool("write_once", {"value": 11}))
    assert recovered.payment_response.error_reason == "settlement_pending"
    assert raw_client.paid_payloads[0] == raw_client.paid_payloads[1]
    assert ReceiptStore(path).list()[0].status_code == 402

    with pytest.raises(ValueError, match="accepted requirements"):
        asyncio.run(client.call_tool("write_once", {"value": 11}))
    assert len(raw_client.paid_payloads) == 2


def test_raw_mcp_session_is_supported_and_requires_explicit_recovery_scope(
    tmp_path,
) -> None:
    class FakeRawSession:
        def __init__(self) -> None:
            self.paid_meta: list[dict[str, object]] = []

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, object] | None = None,
            *,
            meta: dict[str, object] | None = None,
        ) -> object:
            assert name == "generate_report"
            assert arguments == {"topic": "XRPL"}
            if meta is None:
                return SimpleNamespace(
                    content=[],
                    isError=True,
                    structuredContent=challenge(
                        require_payment_identifier=True
                    ).model_dump(by_alias=True, exclude_none=True),
                    meta={},
                )
            self.paid_meta.append(meta)
            settlement = SettleResponse(
                success=True,
                payer="rPayer",
                transaction="4" * 64,
                network="xrpl:1",
                amount="1000",
            )
            return SimpleNamespace(
                content=[],
                isError=False,
                structuredContent=None,
                meta={
                    "x402/payment-response": settlement.model_dump(
                        by_alias=True, exclude_none=True
                    )
                },
            )

    signer = offline_signer()
    session = FakeRawSession()
    with pytest.raises(ValueError, match="recovery_scope must be non-empty"):
        wrap_mcp_client_with_xrpl_payment(
            session,
            signer,
            recovery_scope=" ",
        )
    wrapped = wrap_mcp_client_with_xrpl_payment(
        session,
        signer,
        asset_limits=[
            XRPLAssetSpendLimit(
                network="xrpl:1", asset="XRP", max_amount="1000"
            )
        ],
        store=ReceiptStore(tmp_path / "raw-session.jsonl"),
        recovery_scope="https://mcp.example/sse|principal:user-42",
    )
    result = asyncio.run(
        wrapped.call_tool("generate_report", {"topic": "XRPL"})
    )
    assert result.payment_response.success is True
    assert session.paid_meta[0]["x402/payment"]


def test_receipt_and_attempt_lookup_and_budget_have_no_ten_thousand_cap(
    tmp_path,
) -> None:
    path = tmp_path / "large-history.jsonl"
    store = ReceiptStore(path)
    accepted = challenge().accepts[0]
    oldest = ReceiptRecord(
        created_at="2026-08-30T00:00:00+00:00",
        url="https://merchant.example/paid",
        method="GET",
        status_code=200,
        state="paid",
        settlement=SettleResponse(
            success=True,
            payer="rPayer",
            transaction="3" * 64,
            network="xrpl:1",
            amount="1000",
        ),
        accepted=accepted,
        request_fingerprint="oldest-receipt",
    )
    filler = ReceiptRecord(
        created_at="2026-08-30T00:00:01+00:00",
        url="https://merchant.example/other",
        method="GET",
        status_code=402,
        state="pending",
        settlement=SettleResponse(
            success=False,
            error_reason="settlement_pending",
            transaction="2" * 64,
            network="xrpl:1",
            amount="1000",
        ),
        accepted=accepted,
        request_fingerprint="newer-filler",
    )
    path.write_text(
        oldest.model_dump_json()
        + "\n"
        + (filler.model_dump_json() + "\n") * 10_000
    )

    assert store.latest_for_fingerprint("oldest-receipt") == oldest
    assert store.budget_summary(
        network="xrpl:1", asset="XRP", issuer=None
    )["spent"] == "1000"

    signer = offline_signer()
    payment_client = build_xrpl_payment_client(
        signer,
        asset_limits=[
            XRPLAssetSpendLimit(
                network="xrpl:1", asset="XRP", max_amount="1000"
            )
        ],
    )
    payload = asyncio.run(
        payment_client.create_payment_payload(
            challenge(require_payment_identifier=True)
        )
    )
    claim = store.claim_attempt(
        fingerprint="oldest-attempt",
        payer=signer.classic_address,
        recovery_scope="http",
    )
    store.persist_attempt_payload(
        fingerprint="oldest-attempt",
        owner_token=claim.owner_token,
        payment_payload=payload,
        payment_identifier=extract_payment_identifier(payload),
    )
    index = json.loads(store.index_path.read_text())
    for item in range(10_000):
        fingerprint = f"newer-attempt-{item:05d}"
        index["attempts"][fingerprint] = {
            "fingerprint": fingerprint,
            "payer": signer.classic_address,
            "recovery_scope": "http",
            "owner_token": "abandoned",
            "state": "claimed",
            "lease_expires_at": 0.0,
        }
    expired = dict(index["attempts"]["oldest-attempt"])
    expired.update(
        {
            "fingerprint": "expired-paid",
            "state": "paid",
            "lease_expires_at": 0.0,
        }
    )
    index["attempts"]["expired-paid"] = expired
    store.index_path.write_text(json.dumps(index))

    recovered_attempt = store.get_attempt("oldest-attempt")
    assert recovered_attempt is not None
    assert recovered_attempt.payment_payload == payload
    assert store.prune_expired_attempts() == 1
    assert store.get_attempt("expired-paid") is None
    assert store.get_attempt("oldest-attempt") is not None


def test_receipt_outcomes_dedupe_by_fingerprint_state_and_transaction(
    tmp_path,
) -> None:
    store = ReceiptStore(tmp_path / "dedupe.jsonl")
    accepted = challenge().accepts[0]
    pending = ReceiptRecord(
        created_at="2026-08-30T00:00:00+00:00",
        url="https://merchant.example/paid",
        method="POST",
        status_code=402,
        state="pending",
        settlement=SettleResponse(
            success=False,
            error_reason="settlement_pending",
            transaction="1" * 64,
            network="xrpl:1",
            amount="1000",
        ),
        accepted=accepted,
        request_fingerprint="dedupe-fingerprint",
    )
    paid = pending.model_copy(
        update={
            "created_at": "2026-08-30T00:00:01+00:00",
            "status_code": 200,
            "state": "paid",
            "settlement": SettleResponse(
                success=True,
                payer="rPayer",
                transaction="1" * 64,
                network="xrpl:1",
                amount="1000",
            ),
        }
    )

    assert store.record_outcome(pending) is True
    assert store.record_outcome(pending) is False
    assert store.record_outcome(paid) is True
    assert store.record_outcome(paid) is False
    assert store.record_outcome(pending) is False
    assert [record.state for record in store.list()] == ["paid", "pending"]

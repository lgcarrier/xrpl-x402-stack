from __future__ import annotations

from decimal import Decimal
import os
import re
from typing import Any

import pytest
from x402.schemas import PaymentPayload, PaymentRequirements
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Tx

from devtools.live_testnet_support import (
    LIVE_TEST_FLAG,
    default_rlusd_issuer,
    default_usdc_issuer,
    ensure_rlusd_trustline,
    ensure_usdc_trustline,
    get_demo_wallet_set,
    get_live_wallet_pair,
    get_validated_balance,
    get_validated_trustline_balance,
    get_validated_usdc_trustline_balance,
    resolve_live_testnet_rpc_url,
)
from xrpl_x402_client import XRPLPaymentSigner
from xrpl_x402_core import (
    RLUSD_HEX,
    USDC_HEX,
    invoice_id_to_invoice_id_field,
)
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.replay_store import InMemorySettlementStore
from xrpl_x402_facilitator.xrpl_service import ExactXRPLFacilitatorScheme

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv(LIVE_TEST_FLAG) != "1",
        reason=f"Set {LIVE_TEST_FLAG}=1 to run XRPL Testnet acceptance tests",
    ),
]


@pytest.mark.parametrize("transfer_method", ["sequence", "ticketSequence"])
def test_live_testnet_xrp_exact_v2(transfer_method: str) -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    pair = get_live_wallet_pair(client)
    wallets = sorted(
        pair.as_list(),
        key=lambda wallet: get_validated_balance(client, wallet.classic_address),
        reverse=True,
    )
    if get_validated_balance(client, wallets[0].classic_address) < 2_000_000:
        pytest.skip("Testnet XRP wallet is not funded")
    _exercise_payment(
        client=client,
        rpc_url=rpc_url,
        payer=wallets[0],
        pay_to=wallets[1].classic_address,
        asset="XRP",
        amount="1000",
        issuer=None,
        transfer_method=transfer_method,
    )


@pytest.mark.parametrize("transfer_method", ["sequence", "ticketSequence"])
def test_live_testnet_rlusd_exact_v2(transfer_method: str) -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    issuer = default_rlusd_issuer()
    wallets = get_demo_wallet_set(client)
    candidates = [wallets.merchant_wallet, wallets.buyer_wallet("rlusd")]
    for wallet in candidates:
        ensure_rlusd_trustline(client, wallet, issuer)
    candidates.sort(
        key=lambda wallet: get_validated_trustline_balance(
            client, wallet.classic_address, issuer
        ),
        reverse=True,
    )
    if get_validated_trustline_balance(
        client, candidates[0].classic_address, issuer
    ) < Decimal("0.0001"):
        pytest.skip("RLUSD Testnet wallet is not funded; run devtools.rlusd_fund")
    _exercise_payment(
        client=client,
        rpc_url=rpc_url,
        payer=candidates[0],
        pay_to=candidates[1].classic_address,
        asset=RLUSD_HEX,
        amount="0.0001",
        issuer=issuer,
        transfer_method=transfer_method,
    )


def test_live_testnet_usdc_when_funded() -> None:
    if os.getenv("RUN_XRPL_TESTNET_USDC") != "1":
        pytest.skip("USDC not requested/funded; set RUN_XRPL_TESTNET_USDC=1")
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    issuer = default_usdc_issuer()
    wallets = get_demo_wallet_set(client)
    candidates = [wallets.merchant_wallet, wallets.buyer_wallet("usdc")]
    for wallet in candidates:
        ensure_usdc_trustline(client, wallet, issuer)
    candidates.sort(
        key=lambda wallet: get_validated_usdc_trustline_balance(
            client, wallet.classic_address, issuer
        ),
        reverse=True,
    )
    if get_validated_usdc_trustline_balance(
        client, candidates[0].classic_address, issuer
    ) < Decimal("0.0001"):
        pytest.skip("USDC Testnet wallet is not funded; run devtools.usdc_topup")
    _exercise_payment(
        client=client,
        rpc_url=rpc_url,
        payer=candidates[0],
        pay_to=candidates[1].classic_address,
        asset=USDC_HEX,
        amount="0.0001",
        issuer=issuer,
        transfer_method="sequence",
        allowed_assets=f"USDC:{issuer}",
    )


def _exercise_payment(
    *,
    client: JsonRpcClient,
    rpc_url: str,
    payer: Any,
    pay_to: str,
    asset: str,
    amount: str,
    issuer: str | None,
    transfer_method: str,
    allowed_assets: str = "",
) -> None:
    extra: dict[str, Any] = {
        "areFeesSponsored": False,
        "assetTransferMethod": transfer_method,
        "invoiceId": f"live-{asset}-{transfer_method}",
    }
    if issuer:
        extra["issuer"] = issuer
    requirements = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=asset,
        amount=amount,
        pay_to=pay_to,
        max_timeout_seconds=90,
        extra=extra,
    )
    signer = XRPLPaymentSigner(
        payer,
        client=client,
        network="xrpl:1",
        ticket_inventory_target=1 if transfer_method == "ticketSequence" else 0,
    )
    payload = PaymentPayload(
        accepted=requirements,
        payload=signer.build_x402_payload(requirements),
    )
    mechanism = ExactXRPLFacilitatorScheme(
        Settings(
            _env_file=None,
            XRPL_RPC_URL=rpc_url,
            MY_DESTINATION_ADDRESS=pay_to,
            FACILITATOR_BEARER_TOKEN="live-test-token",
            REDIS_URL="redis://unused:6379/0",
            NETWORK_ID="xrpl:1",
            VALIDATION_TIMEOUT=30,
            ALLOWED_ISSUED_ASSETS=allowed_assets,
        ),
        client=client,
        settlement_store=InMemorySettlementStore(),
    )
    verified = mechanism.verify(payload, requirements)
    assert verified.is_valid is True, verified.invalid_message
    settled = mechanism.settle(payload, requirements)
    assert settled.success is True, settled.error_message
    assert settled.transaction
    assert settled.network == "xrpl:1"
    assert settled.extra == {"status": "validated"}

    transaction_hash = settled.transaction
    assert re.fullmatch(r"[0-9A-Fa-f]{64}", transaction_hash)
    ledger_response = client.request(Tx(transaction=transaction_hash))
    assert ledger_response.is_successful(), ledger_response.result
    ledger_result = ledger_response.result
    ledger_transaction = ledger_result.get("tx_json", ledger_result)
    assert ledger_result.get("validated") is True
    assert ledger_result.get("meta", {}).get("TransactionResult") == "tesSUCCESS"
    assert ledger_transaction.get("Account") == payer.classic_address
    assert ledger_transaction.get("Destination") == pay_to
    assert ledger_transaction.get("InvoiceID") == invoice_id_to_invoice_id_field(
        extra["invoiceId"]
    )

    if transfer_method == "ticketSequence":
        assert ledger_transaction.get("Sequence") == 0
        assert int(ledger_transaction["TicketSequence"]) > 0
    else:
        assert int(ledger_transaction["Sequence"]) > 0
        assert ledger_transaction.get("TicketSequence") is None

    delivered_max = ledger_transaction.get(
        "DeliverMax", ledger_transaction.get("Amount")
    )
    if asset == "XRP":
        assert delivered_max == amount
    else:
        assert delivered_max == {
            "currency": asset,
            "issuer": issuer,
            "value": amount,
        }

    print(
        "validated acceptance"
        f" asset={asset} method={transfer_method}"
        f" tx={transaction_hash}"
        f" ledger={ledger_result.get('ledger_index')}"
    )

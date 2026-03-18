import hashlib
import os
import time
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slowapi import Limiter as SlowLimiter
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Tx
from xrpl.models.transactions import Payment
from xrpl.transaction import autofill, sign
from xrpl.wallet import Wallet

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_core import RLUSD_HEX, USDC_HEX, USDC_TESTNET_ISSUER, normalize_currency_code
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.factory import create_app
from xrpl_x402_facilitator.xrpl_service import XRPLService
from devtools.live_testnet_support import (
    DEFAULT_RLUSD_TESTNET_ISSUER,
    DEFAULT_USDC_TESTNET_ISSUER,
    LIVE_TEST_FLAG,
    RLUSD_TESTNET_ISSUER_ENV,
    USDC_TESTNET_ISSUER_ENV,
    LiveWalletPair,
    consolidate_rlusd_to_wallet_a,
    consolidate_usdc_to_wallet_a,
    ensure_rlusd_trustline,
    ensure_usdc_trustline,
    get_live_wallet_pair,
    get_validated_balance,
    get_validated_trustline_balance,
    get_validated_usdc_trustline_balance,
    recover_tracked_claim_wallets,
    recover_tracked_usdc_claim_wallets,
    resolve_live_testnet_rpc_url,
    wallet_cache_path,
)
from tests.fakes import FakeRedis

XRP_PAYMENT_DROPS = 2_000_000
RLUSD_PAYMENT_VALUE = Decimal("3.75")
USDC_PAYMENT_VALUE = Decimal("4.5")
LIVE_TEST_BEARER_TOKEN = "live-test-facilitator-token"


def _build_live_test_client(app_settings: Settings) -> TestClient:
    redis_client = FakeRedis()
    xrpl_service = XRPLService(app_settings, redis_client=redis_client)
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return TestClient(create_app(app_settings=app_settings, xrpl_service=xrpl_service))
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_xrp_payment_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    wallets = get_live_wallet_pair(client)
    sender, receiver = _select_xrp_wallets(
        client,
        wallets,
        amount_drops=XRP_PAYMENT_DROPS,
    )

    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
    )
    api = _build_live_test_client(app_settings)
    api.headers.update({"Authorization": f"Bearer {LIVE_TEST_BEARER_TOKEN}"})

    receiver_balance_before = get_validated_balance(client, receiver.classic_address)
    invoice_id = hashlib.sha256(f"{time.time_ns()}:{sender.classic_address}".encode()).hexdigest().upper()
    signed_blob, tx_hash = _build_signed_payment_blob(
        client=client,
        sender_address=sender.classic_address,
        destination_address=receiver.classic_address,
        amount=str(XRP_PAYMENT_DROPS),
        invoice_id=invoice_id,
        wallet=sender,
    )

    verify_response = api.post("/verify", json={"signed_tx_blob": signed_blob})
    settle_response = api.post("/settle", json={"signed_tx_blob": signed_blob})
    replay_response = api.post("/verify", json={"signed_tx_blob": signed_blob})

    receiver_balance_after = get_validated_balance(client, receiver.classic_address)
    tx_response = client.request(Tx(transaction=tx_hash)).result
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    ledger_tx_hash = tx_response.get("hash") or tx_payload.get("hash")
    ledger_amount = tx_payload.get("Amount") or tx_payload.get("DeliverMax")

    assert verify_response.status_code == 200
    assert verify_response.json()["invoice_id"] == invoice_id
    assert verify_response.json()["asset"] == {"code": "XRP", "issuer": None}
    assert settle_response.status_code == 200
    assert settle_response.json() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "validated",
    }
    assert replay_response.status_code == 402
    assert "replay attack" in replay_response.json()["detail"].lower()
    assert receiver_balance_after - receiver_balance_before == XRP_PAYMENT_DROPS
    assert tx_response.get("validated") is True
    assert ledger_tx_hash == tx_hash
    assert tx_payload.get("Destination") == receiver.classic_address
    assert ledger_amount == str(XRP_PAYMENT_DROPS)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_rlusd_payment_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    issuer = os.environ.get(RLUSD_TESTNET_ISSUER_ENV, DEFAULT_RLUSD_TESTNET_ISSUER)
    wallets = get_live_wallet_pair(client)
    recover_tracked_claim_wallets(client, wallets.wallet_a, issuer)

    for wallet in wallets.as_list():
        ensure_rlusd_trustline(client, wallet, issuer)

    sender, receiver = _select_rlusd_wallets(
        client,
        wallets,
        issuer,
    )

    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        ALLOWED_ISSUED_ASSETS=f"RLUSD:{issuer}",
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
    )
    api = _build_live_test_client(app_settings)
    api.headers.update({"Authorization": f"Bearer {LIVE_TEST_BEARER_TOKEN}"})

    receiver_balance_before = get_validated_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    invoice_id = hashlib.sha256(f"{time.time_ns()}:{sender.classic_address}".encode()).hexdigest().upper()
    signed_blob, tx_hash = _build_signed_payment_blob(
        client=client,
        sender_address=sender.classic_address,
        destination_address=receiver.classic_address,
        amount={
            "currency": RLUSD_HEX,
            "issuer": issuer,
            "value": str(RLUSD_PAYMENT_VALUE),
        },
        invoice_id=invoice_id,
        wallet=sender,
    )

    verify_response = api.post("/verify", json={"signed_tx_blob": signed_blob})
    settle_response = api.post("/settle", json={"signed_tx_blob": signed_blob})
    replay_response = api.post("/verify", json={"signed_tx_blob": signed_blob})

    receiver_balance_after = get_validated_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    tx_response = client.request(Tx(transaction=tx_hash)).result
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    ledger_tx_hash = tx_response.get("hash") or tx_payload.get("hash")
    ledger_amount = tx_payload.get("Amount") or tx_payload.get("DeliverMax")

    assert verify_response.status_code == 200
    assert verify_response.json()["invoice_id"] == invoice_id
    assert verify_response.json()["amount"] == "3.75 RLUSD"
    assert verify_response.json()["asset"] == {"code": "RLUSD", "issuer": issuer}
    assert settle_response.status_code == 200
    assert settle_response.json() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "validated",
    }
    assert replay_response.status_code == 402
    assert "replay attack" in replay_response.json()["detail"].lower()
    assert receiver_balance_after - receiver_balance_before == RLUSD_PAYMENT_VALUE
    assert tx_response.get("validated") is True
    assert ledger_tx_hash == tx_hash
    assert tx_payload.get("Destination") == receiver.classic_address
    assert isinstance(ledger_amount, dict)
    assert normalize_currency_code(str(ledger_amount["currency"])) == "RLUSD"
    assert ledger_amount["issuer"] == issuer
    assert Decimal(str(ledger_amount["value"])) == RLUSD_PAYMENT_VALUE
    consolidate_rlusd_to_wallet_a(client, wallets, issuer)
    assert get_validated_trustline_balance(client, wallets.wallet_a.classic_address, issuer) >= (
        receiver_balance_after - receiver_balance_before
    )
    assert get_validated_trustline_balance(client, wallets.wallet_b.classic_address, issuer) == Decimal(
        "0"
    )


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_usdc_payment_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    issuer = os.environ.get(USDC_TESTNET_ISSUER_ENV, DEFAULT_USDC_TESTNET_ISSUER)
    wallets = get_live_wallet_pair(client)
    recover_tracked_usdc_claim_wallets(client, wallets.wallet_a, issuer)

    for wallet in wallets.as_list():
        ensure_usdc_trustline(client, wallet, issuer)

    sender, receiver = _select_usdc_wallets(
        client,
        wallets,
        issuer,
    )

    allowed_issued_assets = "" if issuer == USDC_TESTNET_ISSUER else f"USDC:{issuer}"
    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        ALLOWED_ISSUED_ASSETS=allowed_issued_assets,
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
    )
    api = _build_live_test_client(app_settings)
    api.headers.update({"Authorization": f"Bearer {LIVE_TEST_BEARER_TOKEN}"})

    receiver_balance_before = get_validated_usdc_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    invoice_id = hashlib.sha256(f"{time.time_ns()}:{sender.classic_address}".encode()).hexdigest().upper()
    signed_blob, tx_hash = _build_signed_payment_blob(
        client=client,
        sender_address=sender.classic_address,
        destination_address=receiver.classic_address,
        amount={
            "currency": USDC_HEX,
            "issuer": issuer,
            "value": str(USDC_PAYMENT_VALUE),
        },
        invoice_id=invoice_id,
        wallet=sender,
    )

    verify_response = api.post("/verify", json={"signed_tx_blob": signed_blob})
    settle_response = api.post("/settle", json={"signed_tx_blob": signed_blob})
    replay_response = api.post("/verify", json={"signed_tx_blob": signed_blob})

    receiver_balance_after = get_validated_usdc_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    tx_response = client.request(Tx(transaction=tx_hash)).result
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    ledger_tx_hash = tx_response.get("hash") or tx_payload.get("hash")
    ledger_amount = tx_payload.get("Amount") or tx_payload.get("DeliverMax")

    assert verify_response.status_code == 200
    assert verify_response.json()["invoice_id"] == invoice_id
    assert verify_response.json()["amount"] == "4.5 USDC"
    assert verify_response.json()["asset"] == {"code": "USDC", "issuer": issuer}
    assert settle_response.status_code == 200
    assert settle_response.json() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "validated",
    }
    assert replay_response.status_code == 402
    assert "replay attack" in replay_response.json()["detail"].lower()
    assert receiver_balance_after - receiver_balance_before == USDC_PAYMENT_VALUE
    assert tx_response.get("validated") is True
    assert ledger_tx_hash == tx_hash
    assert tx_payload.get("Destination") == receiver.classic_address
    assert isinstance(ledger_amount, dict)
    assert normalize_currency_code(str(ledger_amount["currency"])) == "USDC"
    assert ledger_amount["issuer"] == issuer
    assert Decimal(str(ledger_amount["value"])) == USDC_PAYMENT_VALUE
    consolidate_usdc_to_wallet_a(client, wallets, issuer)
    assert get_validated_usdc_trustline_balance(client, wallets.wallet_a.classic_address, issuer) >= (
        receiver_balance_after - receiver_balance_before
    )
    assert get_validated_usdc_trustline_balance(client, wallets.wallet_b.classic_address, issuer) == Decimal(
        "0"
    )


def _select_xrp_wallets(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    *,
    amount_drops: int,
) -> tuple[Wallet, Wallet]:
    wallet_balances = [
        (wallet, get_validated_balance(client, wallet.classic_address))
        for wallet in wallets.as_list()
    ]
    wallet_balances.sort(key=lambda entry: entry[1], reverse=True)
    sender, sender_balance = wallet_balances[0]
    receiver, _receiver_balance = wallet_balances[1]
    if sender_balance <= amount_drops:
        pytest.skip(
            "Cached XRPL Testnet wallets do not have enough XRP left. "
            f"Delete {wallet_cache_path()} to mint a fresh wallet pair."
        )
    return sender, receiver


def _select_rlusd_wallets(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
) -> tuple[Wallet, Wallet]:
    sender, receiver = _wallet_with_rlusd_liquidity(client, wallets, issuer)
    if sender is not None and receiver is not None:
        return sender, receiver

    pytest.skip(
        "Cached RLUSD test wallets do not have enough balance after tracked-wallet recovery. "
        "Run `python -m devtools.rlusd_topup` to replenish the accumulator and retry."
    )


def _wallet_with_rlusd_liquidity(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
) -> tuple[Wallet | None, Wallet | None]:
    wallet_balances = [
        (wallet, get_validated_trustline_balance(client, wallet.classic_address, issuer))
        for wallet in wallets.as_list()
    ]
    wallet_balances.sort(key=lambda entry: entry[1], reverse=True)
    sender, sender_balance = wallet_balances[0]
    receiver, _receiver_balance = wallet_balances[1]
    if sender_balance >= RLUSD_PAYMENT_VALUE:
        return sender, receiver
    return None, None


def _select_usdc_wallets(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
) -> tuple[Wallet, Wallet]:
    sender, receiver = _wallet_with_usdc_liquidity(client, wallets, issuer)
    if sender is not None and receiver is not None:
        return sender, receiver

    pytest.skip(
        "Cached USDC test wallets do not have enough balance after tracked-wallet recovery. "
        "Run `python -m devtools.usdc_topup` to prepare a manual Circle faucet claim and retry."
    )


def _wallet_with_usdc_liquidity(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
) -> tuple[Wallet | None, Wallet | None]:
    wallet_balances = [
        (wallet, get_validated_usdc_trustline_balance(client, wallet.classic_address, issuer))
        for wallet in wallets.as_list()
    ]
    wallet_balances.sort(key=lambda entry: entry[1], reverse=True)
    sender, sender_balance = wallet_balances[0]
    receiver, _receiver_balance = wallet_balances[1]
    if sender_balance >= USDC_PAYMENT_VALUE:
        return sender, receiver
    return None, None


def _build_signed_payment_blob(
    *,
    client: JsonRpcClient,
    sender_address: str,
    destination_address: str,
    amount: Any,
    invoice_id: str,
    wallet: Any,
) -> tuple[str, str]:
    payment = Payment(
        account=sender_address,
        destination=destination_address,
        amount=amount,
        invoice_id=invoice_id,
    )
    signed_payment = sign(autofill(payment, client), wallet)
    return signed_payment.blob(), signed_payment.get_hash()

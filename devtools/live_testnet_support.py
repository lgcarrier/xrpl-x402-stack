from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountInfo, AccountLines, ServerInfo
from xrpl.models.transactions import AccountDelete, Payment, TrustSet
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet, generate_faucet_wallet

from xrpl_x402_core import (
    RLUSD_CODE,
    RLUSD_HEX,
    USDC_CODE,
    USDC_HEX,
    USDC_TESTNET_ISSUER,
    normalize_currency_code,
)
from xrpl_x402_core.testnet_rpc import resolve_testnet_rpc_url

LIVE_TEST_FLAG = "RUN_XRPL_TESTNET_LIVE"
TRYRLUSD_SESSION_TOKEN_ENV = "TRYRLUSD_SESSION_TOKEN"
XRPL_TESTNET_RPC_URL_ENV = "XRPL_TESTNET_RPC_URL"
RLUSD_TESTNET_ISSUER_ENV = "XRPL_TESTNET_RLUSD_ISSUER"
USDC_TESTNET_ISSUER_ENV = "XRPL_TESTNET_USDC_ISSUER"
WALLET_CACHE_PATH_ENV = "XRPL_TESTNET_WALLET_CACHE_PATH"
DEFAULT_RLUSD_TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
DEFAULT_USDC_TESTNET_ISSUER = USDC_TESTNET_ISSUER
RLUSD_MINT_URL = "https://tryrlusd.com/api/mint-xrpl"
CIRCLE_FAUCET_URL = "https://faucet.circle.com/"
DEFAULT_WALLET_CACHE_PATH = Path(".live-test-wallets/xrpl-testnet-wallets.json")
WALLET_CACHE_VERSION = 1
RLUSD_FAUCET_DRIP_AMOUNT = Decimal("10")
RLUSD_CLAIM_STATE_FILE_NAME = "rlusd-claim-state.json"
RLUSD_CLAIM_STATE_VERSION = 2
RLUSD_CLAIM_COOLDOWN = timedelta(hours=24)
DISPOSABLE_CLAIM_WALLET_USAGE_CONTEXT = "xrpl-x402-facilitator-rlusd-claim"
USDC_FAUCET_DRIP_AMOUNT = Decimal("20")
USDC_CLAIM_STATE_FILE_NAME = "usdc-claim-state.json"
USDC_CLAIM_STATE_VERSION = 1
USDC_CLAIM_COOLDOWN = timedelta(hours=2)
DISPOSABLE_USDC_CLAIM_WALLET_USAGE_CONTEXT = "xrpl-x402-facilitator-usdc-claim"
ACCOUNT_DELETE_LEDGER_GAP = 256
ACCOUNT_DELETE_FEE_FALLBACK_DROPS = 200_000

CLAIM_WALLET_STATUS_CREATED = "created"
CLAIM_WALLET_STATUS_CLAIM_FAILED = "claim_failed"
CLAIM_WALLET_STATUS_CLAIMED = "claimed"
CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING = "awaiting_manual_funding"
CLAIM_WALLET_STATUS_RLUSD_SWEPT = "rlusd_swept"
CLAIM_WALLET_STATUS_USDC_SWEPT = "usdc_swept"
CLAIM_WALLET_STATUS_DELETE_FAILED = "delete_failed"
CLAIM_WALLET_STATUS_DELETED = "deleted"


class RLUSDMintRateLimitedError(RuntimeError):
    """Raised when the RLUSD faucet refuses another claim for now."""


class RLUSDMintRequestError(RuntimeError):
    """Raised when the RLUSD faucet request fails."""


@dataclass(frozen=True)
class LiveWalletPair:
    wallet_a: Wallet
    wallet_b: Wallet

    def as_list(self) -> list[Wallet]:
        return [self.wallet_a, self.wallet_b]


@dataclass
class ClaimWalletState:
    classic_address: str
    seed: str
    created_at: datetime
    trustline_create_tx_hash: str | None = None
    claim_attempted_at: datetime | None = None
    claim_tx_hash: str | None = None
    rlusd_sweep_tx_hash: str | None = None
    trustline_reset_tx_hash: str | None = None
    account_delete_tx_hash: str | None = None
    last_known_rlusd_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    last_known_xrp_balance_drops: int | None = None
    status: str = CLAIM_WALLET_STATUS_CREATED
    last_error: str | None = None
    deleted_at: datetime | None = None


@dataclass
class RLUSDClaimState:
    canonical_wallet_address: str
    issuer: str
    last_successful_session_claim_at: datetime | None = None
    last_successful_session_claim_tx_hash: str | None = None
    last_claim_attempt_at: datetime | None = None
    claim_wallets: list[ClaimWalletState] = field(default_factory=list)


@dataclass(frozen=True)
class ClaimWalletRecoverySummary:
    recovered_rlusd_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    deleted_wallets: int = 0
    processed_wallets: int = 0


@dataclass(frozen=True)
class RLUSDTopUpResult:
    status: str
    canonical_wallet_address: str
    claim_state_path: Path
    message: str
    claim_tx_hash: str | None = None
    cooldown_until: datetime | None = None
    swept_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    deleted_wallets: int = 0
    created_claim_wallet_address: str | None = None


@dataclass
class USDCClaimWalletState:
    classic_address: str
    seed: str
    created_at: datetime
    trustline_create_tx_hash: str | None = None
    usdc_sweep_tx_hash: str | None = None
    trustline_reset_tx_hash: str | None = None
    account_delete_tx_hash: str | None = None
    last_known_usdc_balance: Decimal = field(default_factory=lambda: Decimal("0"))
    last_known_xrp_balance_drops: int | None = None
    status: str = CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING
    last_error: str | None = None
    deleted_at: datetime | None = None


@dataclass
class USDCClaimState:
    canonical_wallet_address: str
    issuer: str
    last_successful_session_claim_at: datetime | None = None
    last_prepared_claim_at: datetime | None = None
    claim_wallets: list[USDCClaimWalletState] = field(default_factory=list)


@dataclass(frozen=True)
class USDCClaimRecoverySummary:
    recovered_usdc_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    deleted_wallets: int = 0
    processed_wallets: int = 0


@dataclass(frozen=True)
class USDCTopUpResult:
    status: str
    canonical_wallet_address: str
    claim_state_path: Path
    message: str
    cooldown_until: datetime | None = None
    swept_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    deleted_wallets: int = 0
    created_claim_wallet_address: str | None = None


def default_rlusd_issuer() -> str:
    return os.environ.get(RLUSD_TESTNET_ISSUER_ENV, DEFAULT_RLUSD_TESTNET_ISSUER)


def default_usdc_issuer() -> str:
    return os.environ.get(USDC_TESTNET_ISSUER_ENV, DEFAULT_USDC_TESTNET_ISSUER)


def resolve_live_testnet_rpc_url(explicit_rpc_url: str | None = None) -> str:
    return resolve_testnet_rpc_url(
        explicit_url=explicit_rpc_url or os.environ.get(XRPL_TESTNET_RPC_URL_ENV)
    )


def wallet_cache_path() -> Path:
    return Path(os.environ.get(WALLET_CACHE_PATH_ENV, DEFAULT_WALLET_CACHE_PATH))


def claim_state_path(cache_path: Path | None = None) -> Path:
    resolved_cache_path = cache_path or wallet_cache_path()
    return resolved_cache_path.parent / RLUSD_CLAIM_STATE_FILE_NAME


def usdc_claim_state_path(cache_path: Path | None = None) -> Path:
    resolved_cache_path = cache_path or wallet_cache_path()
    return resolved_cache_path.parent / USDC_CLAIM_STATE_FILE_NAME


def get_live_wallet_pair(client: JsonRpcClient) -> LiveWalletPair:
    cache_path = wallet_cache_path()
    cached_wallets = _load_cached_wallet_pair(cache_path)
    if cached_wallets is not None and _wallet_pair_is_active(client, cached_wallets):
        return cached_wallets

    fresh_wallets = LiveWalletPair(
        wallet_a=generate_faucet_wallet(
            client,
            usage_context="xrpl-x402-facilitator-live-test",
        ),
        wallet_b=generate_faucet_wallet(
            client,
            usage_context="xrpl-x402-facilitator-live-test",
        ),
    )
    _write_wallet_cache(cache_path, fresh_wallets)
    return fresh_wallets


def get_validated_account_root(client: JsonRpcClient, address: str) -> dict[str, Any] | None:
    response = client.request(AccountInfo(account=address, ledger_index="validated"))
    result = response.result
    if "account_data" not in result:
        return None
    ledger_index = result.get("ledger_index") or result.get("ledger_current_index")
    if ledger_index is None:
        ledger_index = current_validated_ledger_index(client)
    return {
        "account_data": result["account_data"],
        "ledger_index": int(str(ledger_index)),
    }


def get_validated_balance(client: JsonRpcClient, address: str) -> int:
    account_root = get_validated_account_root(client, address)
    if account_root is None:
        raise ValueError(f"XRPL account {address} does not exist")
    return int(str(account_root["account_data"]["Balance"]))


def get_validated_trustline(
    client: JsonRpcClient,
    address: str,
    issuer: str,
    *,
    currency_code: str = RLUSD_CODE,
) -> dict[str, Any] | None:
    response = client.request(AccountLines(account=address, ledger_index="validated"))
    for line in response.result.get("lines", []):
        if (
            line.get("account") == issuer
            and normalize_currency_code(str(line.get("currency", ""))) == currency_code
        ):
            return line
    return None


def get_validated_trustline_balance(
    client: JsonRpcClient,
    address: str,
    issuer: str,
    *,
    currency_code: str = RLUSD_CODE,
) -> Decimal:
    trustline = get_validated_trustline(
        client,
        address,
        issuer,
        currency_code=currency_code,
    )
    if trustline is None:
        return Decimal("0")
    return Decimal(str(trustline["balance"]))


def wait_for_trustline_balance_increase(
    client: JsonRpcClient,
    address: str,
    issuer: str,
    *,
    starting_balance: Decimal,
    increase: Decimal,
    timeout_seconds: int = 30,
    currency_code: str = RLUSD_CODE,
) -> Decimal:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        balance = get_validated_trustline_balance(
            client,
            address,
            issuer,
            currency_code=currency_code,
        )
        if balance >= starting_balance + increase:
            return balance
        time.sleep(1)
    raise AssertionError(
        f"Timed out waiting for {currency_code} balance increase of {increase} on {address}"
    )


def ensure_rlusd_trustline(
    client: JsonRpcClient,
    wallet: Any,
    issuer: str,
    *,
    limit_value: str = "100000",
) -> str | None:
    return _ensure_issued_trustline(
        client,
        wallet,
        issuer,
        currency_code=RLUSD_CODE,
        currency_hex=RLUSD_HEX,
        limit_value=limit_value,
        operation_name="RLUSD trustline transaction",
    )


def trustline_limit_is_sufficient(
    client: JsonRpcClient,
    address: str,
    issuer: str,
    minimum_limit: Decimal,
    *,
    currency_code: str = RLUSD_CODE,
) -> bool:
    trustline = get_validated_trustline(
        client,
        address,
        issuer,
        currency_code=currency_code,
    )
    if trustline is None:
        return False
    return Decimal(str(trustline.get("limit", "0"))) >= minimum_limit


def reset_rlusd_trustline(
    client: JsonRpcClient,
    wallet: Any,
    issuer: str,
) -> str | None:
    return _reset_issued_trustline(
        client,
        wallet,
        issuer,
        currency_code=RLUSD_CODE,
        currency_hex=RLUSD_HEX,
        operation_name="RLUSD trustline reset",
    )


def wait_for_trustline_removal(
    client: JsonRpcClient,
    address: str,
    issuer: str,
    *,
    timeout_seconds: int = 30,
    currency_code: str = RLUSD_CODE,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if get_validated_trustline(
            client,
            address,
            issuer,
            currency_code=currency_code,
        ) is None:
            return True
        time.sleep(1)
    return False


def mint_rlusd_to_address(address: str, session_token: str) -> str:
    request = Request(
        RLUSD_MINT_URL,
        data=json.dumps({"address": address}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"__Secure-next-auth.session-token={session_token}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"error": body}
        if exc.code == 429:
            raise RLUSDMintRateLimitedError(
                f"RLUSD faucet rate limited: {payload.get('error', body)}"
            ) from exc
        raise RLUSDMintRequestError(
            f"RLUSD mint failed with HTTP {exc.code}: {payload.get('error', body)}"
        ) from exc
    except OSError as exc:
        raise RLUSDMintRequestError(f"RLUSD mint failed: {exc}") from exc

    tx_hash = payload.get("txHash")
    if not tx_hash:
        raise RLUSDMintRequestError(f"RLUSD mint did not return a txHash: {payload}")
    return str(tx_hash)


def submit_validated_rlusd_payment(
    client: JsonRpcClient,
    wallet: Any,
    destination_address: str,
    issuer: str,
    amount: Decimal,
) -> str:
    return _submit_validated_issued_payment(
        client,
        wallet,
        destination_address,
        issuer,
        amount,
        currency_hex=RLUSD_HEX,
        operation_name="RLUSD transfer",
    )


def get_validated_usdc_trustline_balance(
    client: JsonRpcClient,
    address: str,
    issuer: str,
) -> Decimal:
    return get_validated_trustline_balance(
        client,
        address,
        issuer,
        currency_code=USDC_CODE,
    )


def ensure_usdc_trustline(
    client: JsonRpcClient,
    wallet: Any,
    issuer: str,
    *,
    limit_value: str = "100000",
) -> str | None:
    return _ensure_issued_trustline(
        client,
        wallet,
        issuer,
        currency_code=USDC_CODE,
        currency_hex=USDC_HEX,
        limit_value=limit_value,
        operation_name="USDC trustline transaction",
    )


def reset_usdc_trustline(
    client: JsonRpcClient,
    wallet: Any,
    issuer: str,
) -> str | None:
    return _reset_issued_trustline(
        client,
        wallet,
        issuer,
        currency_code=USDC_CODE,
        currency_hex=USDC_HEX,
        operation_name="USDC trustline reset",
    )


def submit_validated_usdc_payment(
    client: JsonRpcClient,
    wallet: Any,
    destination_address: str,
    issuer: str,
    amount: Decimal,
) -> str:
    return _submit_validated_issued_payment(
        client,
        wallet,
        destination_address,
        issuer,
        amount,
        currency_hex=USDC_HEX,
        operation_name="USDC transfer",
    )


def _ensure_issued_trustline(
    client: JsonRpcClient,
    wallet: Any,
    issuer: str,
    *,
    currency_code: str,
    currency_hex: str,
    limit_value: str,
    operation_name: str,
) -> str | None:
    if trustline_limit_is_sufficient(
        client,
        wallet.classic_address,
        issuer,
        Decimal(limit_value),
        currency_code=currency_code,
    ):
        return None

    trust_set = TrustSet(
        account=wallet.classic_address,
        flags=262144,
        limit_amount={
            "currency": currency_hex,
            "issuer": issuer,
            "value": limit_value,
        },
    )
    response = submit_and_wait(trust_set, client, wallet).result
    _assert_validated_success(response, operation_name)
    return _response_tx_hash(response)


def _reset_issued_trustline(
    client: JsonRpcClient,
    wallet: Any,
    issuer: str,
    *,
    currency_code: str,
    currency_hex: str,
    operation_name: str,
) -> str | None:
    trustline = get_validated_trustline(
        client,
        wallet.classic_address,
        issuer,
        currency_code=currency_code,
    )
    if trustline is None:
        return None

    balance = Decimal(str(trustline.get("balance", "0")))
    limit = Decimal(str(trustline.get("limit", "0")))
    if balance != 0:
        raise RuntimeError(
            f"Cannot reset {currency_code} trustline for {wallet.classic_address} while balance is {balance}"
        )
    if limit == 0:
        return None

    trust_set = TrustSet(
        account=wallet.classic_address,
        limit_amount={
            "currency": currency_hex,
            "issuer": issuer,
            "value": "0",
        },
    )
    response = submit_and_wait(trust_set, client, wallet).result
    _assert_validated_success(response, operation_name)
    return _response_tx_hash(response)


def _submit_validated_issued_payment(
    client: JsonRpcClient,
    wallet: Any,
    destination_address: str,
    issuer: str,
    amount: Decimal,
    *,
    currency_hex: str,
    operation_name: str,
) -> str:
    payment = Payment(
        account=wallet.classic_address,
        destination=destination_address,
        amount={
            "currency": currency_hex,
            "issuer": issuer,
            "value": _decimal_to_value(amount),
        },
    )
    response = submit_and_wait(payment, client, wallet).result
    _assert_validated_success(response, operation_name)
    return _response_tx_hash(response)


def submit_validated_account_delete(
    client: JsonRpcClient,
    wallet: Any,
    destination_address: str,
) -> str:
    account_delete = AccountDelete(
        account=wallet.classic_address,
        destination=destination_address,
        fee=str(account_delete_fee_drops(client)),
    )
    response = submit_and_wait(
        account_delete,
        client,
        wallet,
        check_fee=False,
        fail_hard=True,
    ).result
    _assert_validated_success(response, "AccountDelete")
    return _response_tx_hash(response)


def account_delete_fee_drops(client: JsonRpcClient) -> int:
    try:
        response = client.request(ServerInfo())
        validated_ledger = response.result.get("info", {}).get("validated_ledger", {})
        reserve_inc_xrp = validated_ledger.get("reserve_inc_xrp")
        if reserve_inc_xrp is None:
            raise KeyError("reserve_inc_xrp missing")
        return int((Decimal(str(reserve_inc_xrp)) * Decimal("1000000")).to_integral_value())
    except Exception:
        return ACCOUNT_DELETE_FEE_FALLBACK_DROPS


def next_rlusd_claim_time(state: RLUSDClaimState) -> datetime | None:
    if state.last_successful_session_claim_at is None:
        return None
    return state.last_successful_session_claim_at + RLUSD_CLAIM_COOLDOWN


def next_usdc_claim_time(state: USDCClaimState) -> datetime | None:
    if state.last_successful_session_claim_at is None:
        return None
    return state.last_successful_session_claim_at + USDC_CLAIM_COOLDOWN


def load_rlusd_claim_state(
    path: Path,
    canonical_wallet_address: str,
    issuer: str,
) -> RLUSDClaimState:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return RLUSDClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )
    except json.JSONDecodeError:
        return RLUSDClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )

    version = payload.get("version")
    if version == 1:
        if payload.get("canonical_wallet_address") != canonical_wallet_address:
            return RLUSDClaimState(
                canonical_wallet_address=canonical_wallet_address,
                issuer=issuer,
            )
        if payload.get("issuer") != issuer:
            return RLUSDClaimState(
                canonical_wallet_address=canonical_wallet_address,
                issuer=issuer,
            )
        last_claim_at = _deserialize_datetime(payload.get("last_successful_claim_at"))
        return RLUSDClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
            last_successful_session_claim_at=last_claim_at,
            last_successful_session_claim_tx_hash=payload.get("last_successful_claim_tx_hash"),
        )

    if version != RLUSD_CLAIM_STATE_VERSION:
        return RLUSDClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )

    if payload.get("canonical_wallet_address") != canonical_wallet_address:
        return RLUSDClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )
    if payload.get("issuer") != issuer:
        return RLUSDClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )

    claim_wallets = [
        _deserialize_claim_wallet_state(item)
        for item in payload.get("claim_wallets", [])
        if isinstance(item, dict)
    ]
    return RLUSDClaimState(
        canonical_wallet_address=canonical_wallet_address,
        issuer=issuer,
        last_successful_session_claim_at=_deserialize_datetime(
            payload.get("last_successful_session_claim_at")
        ),
        last_successful_session_claim_tx_hash=payload.get("last_successful_session_claim_tx_hash"),
        last_claim_attempt_at=_deserialize_datetime(payload.get("last_claim_attempt_at")),
        claim_wallets=claim_wallets,
    )


def write_rlusd_claim_state(path: Path, state: RLUSDClaimState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": RLUSD_CLAIM_STATE_VERSION,
        "canonical_wallet_address": state.canonical_wallet_address,
        "issuer": state.issuer,
        "last_successful_session_claim_at": _serialize_datetime(
            state.last_successful_session_claim_at
        ),
        "last_successful_session_claim_tx_hash": state.last_successful_session_claim_tx_hash,
        "last_claim_attempt_at": _serialize_datetime(state.last_claim_attempt_at),
        "claim_wallets": [_serialize_claim_wallet_state(item) for item in state.claim_wallets],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_usdc_claim_state(
    path: Path,
    canonical_wallet_address: str,
    issuer: str,
) -> USDCClaimState:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return USDCClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )
    except json.JSONDecodeError:
        return USDCClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )

    if payload.get("version") != USDC_CLAIM_STATE_VERSION:
        return USDCClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )
    if payload.get("canonical_wallet_address") != canonical_wallet_address:
        return USDCClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )
    if payload.get("issuer") != issuer:
        return USDCClaimState(
            canonical_wallet_address=canonical_wallet_address,
            issuer=issuer,
        )

    claim_wallets = [
        _deserialize_usdc_claim_wallet_state(item)
        for item in payload.get("claim_wallets", [])
        if isinstance(item, dict)
    ]
    return USDCClaimState(
        canonical_wallet_address=canonical_wallet_address,
        issuer=issuer,
        last_successful_session_claim_at=_deserialize_datetime(
            payload.get("last_successful_session_claim_at")
        ),
        last_prepared_claim_at=_deserialize_datetime(payload.get("last_prepared_claim_at")),
        claim_wallets=claim_wallets,
    )


def write_usdc_claim_state(path: Path, state: USDCClaimState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": USDC_CLAIM_STATE_VERSION,
        "canonical_wallet_address": state.canonical_wallet_address,
        "issuer": state.issuer,
        "last_successful_session_claim_at": _serialize_datetime(
            state.last_successful_session_claim_at
        ),
        "last_prepared_claim_at": _serialize_datetime(state.last_prepared_claim_at),
        "claim_wallets": [_serialize_usdc_claim_wallet_state(item) for item in state.claim_wallets],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def recover_tracked_claim_wallets(
    client: JsonRpcClient,
    accumulator_wallet: Any,
    issuer: str,
    *,
    claim_state_file: Path | None = None,
    now: datetime | None = None,
) -> tuple[RLUSDClaimState, ClaimWalletRecoverySummary]:
    state_path = claim_state_file or claim_state_path()
    state = load_rlusd_claim_state(state_path, accumulator_wallet.classic_address, issuer)
    summary = ClaimWalletRecoverySummary()
    changed = False

    for claim_wallet in state.claim_wallets:
        if claim_wallet.status == CLAIM_WALLET_STATUS_DELETED:
            continue
        wallet_now = now or datetime.now(timezone.utc)
        claim_wallet_changed, wallet_summary = _recover_claim_wallet(
            client,
            claim_wallet,
            accumulator_wallet,
            issuer,
            now=wallet_now,
        )
        if claim_wallet_changed:
            changed = True
            write_rlusd_claim_state(state_path, state)
        summary = ClaimWalletRecoverySummary(
            recovered_rlusd_amount=summary.recovered_rlusd_amount + wallet_summary.recovered_rlusd_amount,
            deleted_wallets=summary.deleted_wallets + wallet_summary.deleted_wallets,
            processed_wallets=summary.processed_wallets + wallet_summary.processed_wallets,
        )

    if changed and not state_path.exists():
        write_rlusd_claim_state(state_path, state)
    return state, summary


def claim_rlusd_topup(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
    *,
    session_token: str | None,
    now: datetime | None = None,
    claim_state_file: Path | None = None,
) -> RLUSDTopUpResult:
    state_path = claim_state_file or claim_state_path()
    claim_started_at = now or datetime.now(timezone.utc)
    state, recovery = recover_tracked_claim_wallets(
        client,
        wallets.wallet_a,
        issuer,
        claim_state_file=state_path,
        now=claim_started_at,
    )
    ensure_rlusd_trustline(client, wallets.wallet_a, issuer)

    cooldown_until = next_rlusd_claim_time(state)
    if cooldown_until is not None and claim_started_at < cooldown_until:
        return RLUSDTopUpResult(
            status="cooldown",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            cooldown_until=cooldown_until,
            swept_amount=recovery.recovered_rlusd_amount,
            deleted_wallets=recovery.deleted_wallets,
            message=f"Skipping RLUSD claim until {cooldown_until.isoformat()}",
        )

    if not session_token:
        return RLUSDTopUpResult(
            status="missing_token",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            swept_amount=recovery.recovered_rlusd_amount,
            deleted_wallets=recovery.deleted_wallets,
            message=(
                f"Recovered tracked claim wallets. Set {TRYRLUSD_SESSION_TOKEN_ENV} to "
                "attempt another RLUSD claim."
            ),
        )

    state.last_claim_attempt_at = claim_started_at
    write_rlusd_claim_state(state_path, state)

    claim_wallet = generate_faucet_wallet(
        client,
        usage_context=DISPOSABLE_CLAIM_WALLET_USAGE_CONTEXT,
    )
    if claim_wallet.seed is None:
        raise RuntimeError("Disposable claim wallet is missing a seed")

    claim_wallet_state = ClaimWalletState(
        classic_address=claim_wallet.classic_address,
        seed=claim_wallet.seed,
        created_at=claim_started_at,
    )
    state.claim_wallets.append(claim_wallet_state)
    write_rlusd_claim_state(state_path, state)

    try:
        claim_wallet_state.trustline_create_tx_hash = ensure_rlusd_trustline(client, claim_wallet, issuer)
        claim_wallet_state.claim_attempted_at = claim_started_at
        write_rlusd_claim_state(state_path, state)

        claim_tx_hash = mint_rlusd_to_address(claim_wallet.classic_address, session_token)
        claim_wallet_state.claim_tx_hash = claim_tx_hash
        claim_wallet_state.status = CLAIM_WALLET_STATUS_CLAIMED
        claim_wallet_state.last_error = None
        write_rlusd_claim_state(state_path, state)

        wait_for_trustline_balance_increase(
            client,
            claim_wallet.classic_address,
            issuer,
            starting_balance=Decimal("0"),
            increase=RLUSD_FAUCET_DRIP_AMOUNT,
        )

        state.last_successful_session_claim_at = claim_started_at
        state.last_successful_session_claim_tx_hash = claim_tx_hash
        write_rlusd_claim_state(state_path, state)

        _recover_claim_wallet(
            client,
            claim_wallet_state,
            wallets.wallet_a,
            issuer,
            now=claim_started_at,
        )
        write_rlusd_claim_state(state_path, state)

        return RLUSDTopUpResult(
            status="claimed",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            claim_tx_hash=claim_tx_hash,
            cooldown_until=next_rlusd_claim_time(state),
            swept_amount=recovery.recovered_rlusd_amount + RLUSD_FAUCET_DRIP_AMOUNT,
            deleted_wallets=recovery.deleted_wallets + int(
                claim_wallet_state.status == CLAIM_WALLET_STATUS_DELETED
            ),
            created_claim_wallet_address=claim_wallet.classic_address,
            message=f"Claimed {RLUSD_FAUCET_DRIP_AMOUNT} RLUSD to {claim_wallet.classic_address}",
        )
    except RLUSDMintRateLimitedError as exc:
        claim_wallet_state.claim_attempted_at = claim_started_at
        claim_wallet_state.status = CLAIM_WALLET_STATUS_CLAIM_FAILED
        claim_wallet_state.last_error = str(exc)
        _recover_claim_wallet(
            client,
            claim_wallet_state,
            wallets.wallet_a,
            issuer,
            now=claim_started_at,
        )
        write_rlusd_claim_state(state_path, state)
        return RLUSDTopUpResult(
            status="rate_limited",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            swept_amount=recovery.recovered_rlusd_amount,
            deleted_wallets=recovery.deleted_wallets,
            created_claim_wallet_address=claim_wallet.classic_address,
            message=str(exc),
        )
    except Exception as exc:
        claim_wallet_state.claim_attempted_at = claim_started_at
        claim_wallet_state.status = CLAIM_WALLET_STATUS_CLAIM_FAILED
        claim_wallet_state.last_error = str(exc)
        _recover_claim_wallet(
            client,
            claim_wallet_state,
            wallets.wallet_a,
            issuer,
            now=claim_started_at,
        )
        write_rlusd_claim_state(state_path, state)
        return RLUSDTopUpResult(
            status="claim_failed",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            swept_amount=recovery.recovered_rlusd_amount,
            deleted_wallets=recovery.deleted_wallets,
            created_claim_wallet_address=claim_wallet.classic_address,
            message=f"RLUSD top-up failed: {exc}",
        )


def consolidate_rlusd_to_wallet_a(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
) -> Decimal:
    secondary_balance = get_validated_trustline_balance(
        client,
        wallets.wallet_b.classic_address,
        issuer,
    )
    if secondary_balance <= 0:
        return Decimal("0")

    ensure_rlusd_trustline(client, wallets.wallet_a, issuer)
    starting_balance = get_validated_trustline_balance(
        client,
        wallets.wallet_a.classic_address,
        issuer,
    )
    submit_validated_rlusd_payment(
        client,
        wallets.wallet_b,
        wallets.wallet_a.classic_address,
        issuer,
        secondary_balance,
    )
    wait_for_trustline_balance_increase(
        client,
        wallets.wallet_a.classic_address,
        issuer,
        starting_balance=starting_balance,
        increase=secondary_balance,
    )
    return secondary_balance


def recover_tracked_usdc_claim_wallets(
    client: JsonRpcClient,
    accumulator_wallet: Any,
    issuer: str,
    *,
    claim_state_file: Path | None = None,
    now: datetime | None = None,
) -> tuple[USDCClaimState, USDCClaimRecoverySummary]:
    state_path = claim_state_file or usdc_claim_state_path()
    state = load_usdc_claim_state(state_path, accumulator_wallet.classic_address, issuer)
    summary = USDCClaimRecoverySummary()
    changed = False
    recovery_time = now or datetime.now(timezone.utc)

    for claim_wallet in state.claim_wallets:
        if claim_wallet.status == CLAIM_WALLET_STATUS_DELETED:
            continue
        claim_wallet_changed, wallet_summary = _recover_usdc_claim_wallet(
            client,
            claim_wallet,
            accumulator_wallet,
            issuer,
            now=recovery_time,
        )
        if claim_wallet_changed:
            changed = True
            write_usdc_claim_state(state_path, state)
        summary = USDCClaimRecoverySummary(
            recovered_usdc_amount=summary.recovered_usdc_amount + wallet_summary.recovered_usdc_amount,
            deleted_wallets=summary.deleted_wallets + wallet_summary.deleted_wallets,
            processed_wallets=summary.processed_wallets + wallet_summary.processed_wallets,
        )

    if summary.recovered_usdc_amount > 0:
        state.last_successful_session_claim_at = recovery_time
        changed = True

    if changed:
        write_usdc_claim_state(state_path, state)
    return state, summary


def prepare_usdc_topup(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
    *,
    now: datetime | None = None,
    claim_state_file: Path | None = None,
) -> USDCTopUpResult:
    state_path = claim_state_file or usdc_claim_state_path()
    prepared_at = now or datetime.now(timezone.utc)
    state, recovery = recover_tracked_usdc_claim_wallets(
        client,
        wallets.wallet_a,
        issuer,
        claim_state_file=state_path,
        now=prepared_at,
    )
    ensure_usdc_trustline(client, wallets.wallet_a, issuer)

    pending_wallet = next(
        (
            claim_wallet
            for claim_wallet in state.claim_wallets
            if claim_wallet.status == CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING
        ),
        None,
    )
    if pending_wallet is not None:
        return USDCTopUpResult(
            status="awaiting_manual_claim",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            swept_amount=recovery.recovered_usdc_amount,
            deleted_wallets=recovery.deleted_wallets,
            created_claim_wallet_address=pending_wallet.classic_address,
            message=_format_usdc_manual_claim_message(pending_wallet.classic_address),
        )

    cooldown_until = next_usdc_claim_time(state)
    if cooldown_until is not None and prepared_at < cooldown_until:
        return USDCTopUpResult(
            status="cooldown",
            canonical_wallet_address=wallets.wallet_a.classic_address,
            claim_state_path=state_path,
            cooldown_until=cooldown_until,
            swept_amount=recovery.recovered_usdc_amount,
            deleted_wallets=recovery.deleted_wallets,
            message=f"Skipping USDC claim wallet creation until {cooldown_until.isoformat()}",
        )

    claim_wallet = generate_faucet_wallet(
        client,
        usage_context=DISPOSABLE_USDC_CLAIM_WALLET_USAGE_CONTEXT,
    )
    if claim_wallet.seed is None:
        raise RuntimeError("Disposable USDC claim wallet is missing a seed")

    claim_wallet_state = USDCClaimWalletState(
        classic_address=claim_wallet.classic_address,
        seed=claim_wallet.seed,
        created_at=prepared_at,
    )
    claim_wallet_state.trustline_create_tx_hash = ensure_usdc_trustline(
        client,
        claim_wallet,
        issuer,
    )
    state.claim_wallets.append(claim_wallet_state)
    state.last_prepared_claim_at = prepared_at
    write_usdc_claim_state(state_path, state)

    return USDCTopUpResult(
        status="awaiting_manual_claim",
        canonical_wallet_address=wallets.wallet_a.classic_address,
        claim_state_path=state_path,
        swept_amount=recovery.recovered_usdc_amount,
        deleted_wallets=recovery.deleted_wallets,
        created_claim_wallet_address=claim_wallet.classic_address,
        message=_format_usdc_manual_claim_message(claim_wallet.classic_address),
    )


def consolidate_usdc_to_wallet_a(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    issuer: str,
) -> Decimal:
    secondary_balance = get_validated_usdc_trustline_balance(
        client,
        wallets.wallet_b.classic_address,
        issuer,
    )
    if secondary_balance <= 0:
        return Decimal("0")

    ensure_usdc_trustline(client, wallets.wallet_a, issuer)
    starting_balance = get_validated_usdc_trustline_balance(
        client,
        wallets.wallet_a.classic_address,
        issuer,
    )
    submit_validated_usdc_payment(
        client,
        wallets.wallet_b,
        wallets.wallet_a.classic_address,
        issuer,
        secondary_balance,
    )
    wait_for_trustline_balance_increase(
        client,
        wallets.wallet_a.classic_address,
        issuer,
        starting_balance=starting_balance,
        increase=secondary_balance,
        currency_code=USDC_CODE,
    )
    return secondary_balance


def _recover_usdc_claim_wallet(
    client: JsonRpcClient,
    claim_wallet: USDCClaimWalletState,
    accumulator_wallet: Any,
    issuer: str,
    *,
    now: datetime,
) -> tuple[bool, USDCClaimRecoverySummary]:
    changed = False
    recovered_usdc = Decimal("0")
    deleted_wallets = 0

    try:
        wallet = Wallet.from_seed(claim_wallet.seed)
    except Exception as exc:
        if claim_wallet.status != CLAIM_WALLET_STATUS_DELETE_FAILED or claim_wallet.last_error != str(exc):
            claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
            claim_wallet.last_error = str(exc)
            changed = True
        return changed, USDCClaimRecoverySummary(processed_wallets=1)

    account_root = get_validated_account_root(client, claim_wallet.classic_address)
    if account_root is None:
        changed |= _mark_usdc_claim_wallet_deleted(claim_wallet, now)
        return changed, USDCClaimRecoverySummary(deleted_wallets=1, processed_wallets=1)

    current_xrp_balance = int(str(account_root["account_data"]["Balance"]))
    if claim_wallet.last_known_xrp_balance_drops != current_xrp_balance:
        claim_wallet.last_known_xrp_balance_drops = current_xrp_balance
        changed = True

    usdc_balance = get_validated_usdc_trustline_balance(client, claim_wallet.classic_address, issuer)
    if claim_wallet.last_known_usdc_balance != usdc_balance:
        claim_wallet.last_known_usdc_balance = usdc_balance
        changed = True

    if (
        claim_wallet.status == CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING
        and usdc_balance <= 0
    ):
        return changed, USDCClaimRecoverySummary(processed_wallets=1)

    if usdc_balance > 0:
        ensure_usdc_trustline(client, accumulator_wallet, issuer)
        starting_balance = get_validated_usdc_trustline_balance(
            client,
            accumulator_wallet.classic_address,
            issuer,
        )
        claim_wallet.usdc_sweep_tx_hash = submit_validated_usdc_payment(
            client,
            wallet,
            accumulator_wallet.classic_address,
            issuer,
            usdc_balance,
        )
        wait_for_trustline_balance_increase(
            client,
            accumulator_wallet.classic_address,
            issuer,
            starting_balance=starting_balance,
            increase=usdc_balance,
            currency_code=USDC_CODE,
        )
        claim_wallet.last_known_usdc_balance = Decimal("0")
        claim_wallet.status = CLAIM_WALLET_STATUS_USDC_SWEPT
        claim_wallet.last_error = None
        changed = True
        recovered_usdc += usdc_balance

    trustline = get_validated_trustline(
        client,
        claim_wallet.classic_address,
        issuer,
        currency_code=USDC_CODE,
    )
    if trustline is not None:
        trustline_balance = Decimal(str(trustline.get("balance", "0")))
        if trustline_balance > 0:
            claim_wallet.last_known_usdc_balance = trustline_balance
            claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
            claim_wallet.last_error = (
                f"USDC trustline for {claim_wallet.classic_address} still has balance {trustline_balance}"
            )
            changed = True
            return changed, USDCClaimRecoverySummary(
                recovered_usdc_amount=recovered_usdc,
                processed_wallets=1,
            )

        trustline_reset_tx_hash = reset_usdc_trustline(client, wallet, issuer)
        if trustline_reset_tx_hash is not None:
            claim_wallet.trustline_reset_tx_hash = trustline_reset_tx_hash
            changed = True
        if not wait_for_trustline_removal(
            client,
            claim_wallet.classic_address,
            issuer,
            currency_code=USDC_CODE,
        ):
            claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
            claim_wallet.last_error = (
                f"USDC trustline for {claim_wallet.classic_address} still blocks AccountDelete"
            )
            changed = True
            return changed, USDCClaimRecoverySummary(
                recovered_usdc_amount=recovered_usdc,
                processed_wallets=1,
            )

    account_root = get_validated_account_root(client, claim_wallet.classic_address)
    if account_root is None:
        changed |= _mark_usdc_claim_wallet_deleted(claim_wallet, now)
        return changed, USDCClaimRecoverySummary(
            recovered_usdc_amount=recovered_usdc,
            deleted_wallets=1,
            processed_wallets=1,
        )

    claim_wallet.last_known_xrp_balance_drops = int(str(account_root["account_data"]["Balance"]))
    if not account_delete_is_ready(account_root):
        next_ledger = next_account_delete_eligible_ledger(account_root)
        if recovered_usdc > 0:
            claim_wallet.status = CLAIM_WALLET_STATUS_USDC_SWEPT
        claim_wallet.last_error = (
            f"AccountDelete not yet eligible until validated ledger {next_ledger}"
        )
        changed = True
        return changed, USDCClaimRecoverySummary(
            recovered_usdc_amount=recovered_usdc,
            processed_wallets=1,
        )

    try:
        claim_wallet.account_delete_tx_hash = submit_validated_account_delete(
            client,
            wallet,
            accumulator_wallet.classic_address,
        )
    except Exception as exc:
        claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
        claim_wallet.last_error = str(exc)
        changed = True
        return changed, USDCClaimRecoverySummary(
            recovered_usdc_amount=recovered_usdc,
            processed_wallets=1,
        )

    changed |= _mark_usdc_claim_wallet_deleted(claim_wallet, now)
    deleted_wallets += 1
    return changed, USDCClaimRecoverySummary(
        recovered_usdc_amount=recovered_usdc,
        deleted_wallets=deleted_wallets,
        processed_wallets=1,
    )


def _recover_claim_wallet(
    client: JsonRpcClient,
    claim_wallet: ClaimWalletState,
    accumulator_wallet: Any,
    issuer: str,
    *,
    now: datetime,
) -> tuple[bool, ClaimWalletRecoverySummary]:
    changed = False
    recovered_rlusd = Decimal("0")
    deleted_wallets = 0

    try:
        wallet = Wallet.from_seed(claim_wallet.seed)
    except Exception as exc:
        if claim_wallet.status != CLAIM_WALLET_STATUS_DELETE_FAILED or claim_wallet.last_error != str(exc):
            claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
            claim_wallet.last_error = str(exc)
            changed = True
        return changed, ClaimWalletRecoverySummary(processed_wallets=1)

    account_root = get_validated_account_root(client, claim_wallet.classic_address)
    if account_root is None:
        changed |= _mark_claim_wallet_deleted(claim_wallet, now)
        return changed, ClaimWalletRecoverySummary(deleted_wallets=1, processed_wallets=1)

    current_xrp_balance = int(str(account_root["account_data"]["Balance"]))
    if claim_wallet.last_known_xrp_balance_drops != current_xrp_balance:
        claim_wallet.last_known_xrp_balance_drops = current_xrp_balance
        changed = True

    rlusd_balance = get_validated_trustline_balance(client, claim_wallet.classic_address, issuer)
    if claim_wallet.last_known_rlusd_balance != rlusd_balance:
        claim_wallet.last_known_rlusd_balance = rlusd_balance
        changed = True

    if rlusd_balance > 0:
        ensure_rlusd_trustline(client, accumulator_wallet, issuer)
        starting_balance = get_validated_trustline_balance(
            client,
            accumulator_wallet.classic_address,
            issuer,
        )
        claim_wallet.rlusd_sweep_tx_hash = submit_validated_rlusd_payment(
            client,
            wallet,
            accumulator_wallet.classic_address,
            issuer,
            rlusd_balance,
        )
        wait_for_trustline_balance_increase(
            client,
            accumulator_wallet.classic_address,
            issuer,
            starting_balance=starting_balance,
            increase=rlusd_balance,
        )
        claim_wallet.last_known_rlusd_balance = Decimal("0")
        claim_wallet.status = CLAIM_WALLET_STATUS_RLUSD_SWEPT
        claim_wallet.last_error = None
        changed = True
        recovered_rlusd += rlusd_balance

    trustline = get_validated_trustline(client, claim_wallet.classic_address, issuer)
    if trustline is not None:
        trustline_balance = Decimal(str(trustline.get("balance", "0")))
        if trustline_balance > 0:
            claim_wallet.last_known_rlusd_balance = trustline_balance
            claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
            claim_wallet.last_error = (
                f"RLUSD trustline for {claim_wallet.classic_address} still has balance {trustline_balance}"
            )
            changed = True
            return changed, ClaimWalletRecoverySummary(
                recovered_rlusd_amount=recovered_rlusd,
                processed_wallets=1,
            )

        trustline_reset_tx_hash = reset_rlusd_trustline(client, wallet, issuer)
        if trustline_reset_tx_hash is not None:
            claim_wallet.trustline_reset_tx_hash = trustline_reset_tx_hash
            changed = True
        if not wait_for_trustline_removal(client, claim_wallet.classic_address, issuer):
            claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
            claim_wallet.last_error = (
                f"RLUSD trustline for {claim_wallet.classic_address} still blocks AccountDelete"
            )
            changed = True
            return changed, ClaimWalletRecoverySummary(
                recovered_rlusd_amount=recovered_rlusd,
                processed_wallets=1,
            )

    account_root = get_validated_account_root(client, claim_wallet.classic_address)
    if account_root is None:
        changed |= _mark_claim_wallet_deleted(claim_wallet, now)
        return changed, ClaimWalletRecoverySummary(
            recovered_rlusd_amount=recovered_rlusd,
            deleted_wallets=1,
            processed_wallets=1,
        )

    claim_wallet.last_known_xrp_balance_drops = int(str(account_root["account_data"]["Balance"]))
    if not account_delete_is_ready(account_root):
        next_ledger = next_account_delete_eligible_ledger(account_root)
        if claim_wallet.claim_tx_hash is not None:
            claim_wallet.status = CLAIM_WALLET_STATUS_RLUSD_SWEPT
        claim_wallet.last_error = (
            f"AccountDelete not yet eligible until validated ledger {next_ledger}"
        )
        changed = True
        return changed, ClaimWalletRecoverySummary(
            recovered_rlusd_amount=recovered_rlusd,
            processed_wallets=1,
        )

    try:
        claim_wallet.account_delete_tx_hash = submit_validated_account_delete(
            client,
            wallet,
            accumulator_wallet.classic_address,
        )
    except Exception as exc:
        claim_wallet.status = CLAIM_WALLET_STATUS_DELETE_FAILED
        claim_wallet.last_error = str(exc)
        changed = True
        return changed, ClaimWalletRecoverySummary(
            recovered_rlusd_amount=recovered_rlusd,
            processed_wallets=1,
        )

    changed |= _mark_claim_wallet_deleted(claim_wallet, now)
    deleted_wallets += 1
    return changed, ClaimWalletRecoverySummary(
        recovered_rlusd_amount=recovered_rlusd,
        deleted_wallets=deleted_wallets,
        processed_wallets=1,
    )


def account_delete_is_ready(account_root: dict[str, Any]) -> bool:
    sequence = int(str(account_root["account_data"]["Sequence"]))
    validated_ledger = int(str(account_root["ledger_index"]))
    return validated_ledger > sequence + ACCOUNT_DELETE_LEDGER_GAP


def next_account_delete_eligible_ledger(account_root: dict[str, Any]) -> int:
    sequence = int(str(account_root["account_data"]["Sequence"]))
    return sequence + ACCOUNT_DELETE_LEDGER_GAP + 1


def current_validated_ledger_index(client: JsonRpcClient) -> int:
    response = client.request(ServerInfo())
    validated_ledger = response.result.get("info", {}).get("validated_ledger", {})
    ledger_index = validated_ledger.get("seq")
    if ledger_index is None:
        raise RuntimeError("XRPL server info did not include validated ledger index")
    return int(str(ledger_index))


def _deserialize_claim_wallet_state(payload: dict[str, Any]) -> ClaimWalletState:
    return ClaimWalletState(
        classic_address=str(payload["classic_address"]),
        seed=str(payload["seed"]),
        created_at=_deserialize_datetime(payload.get("created_at")) or datetime.now(timezone.utc),
        trustline_create_tx_hash=payload.get("trustline_create_tx_hash"),
        claim_attempted_at=_deserialize_datetime(payload.get("claim_attempted_at")),
        claim_tx_hash=payload.get("claim_tx_hash"),
        rlusd_sweep_tx_hash=payload.get("rlusd_sweep_tx_hash"),
        trustline_reset_tx_hash=payload.get("trustline_reset_tx_hash"),
        account_delete_tx_hash=payload.get("account_delete_tx_hash"),
        last_known_rlusd_balance=Decimal(str(payload.get("last_known_rlusd_balance", "0"))),
        last_known_xrp_balance_drops=(
            int(payload["last_known_xrp_balance_drops"])
            if payload.get("last_known_xrp_balance_drops") is not None
            else None
        ),
        status=str(payload.get("status", CLAIM_WALLET_STATUS_CREATED)),
        last_error=payload.get("last_error"),
        deleted_at=_deserialize_datetime(payload.get("deleted_at")),
    )


def _serialize_claim_wallet_state(item: ClaimWalletState) -> dict[str, Any]:
    return {
        "classic_address": item.classic_address,
        "seed": item.seed,
        "created_at": _serialize_datetime(item.created_at),
        "trustline_create_tx_hash": item.trustline_create_tx_hash,
        "claim_attempted_at": _serialize_datetime(item.claim_attempted_at),
        "claim_tx_hash": item.claim_tx_hash,
        "rlusd_sweep_tx_hash": item.rlusd_sweep_tx_hash,
        "trustline_reset_tx_hash": item.trustline_reset_tx_hash,
        "account_delete_tx_hash": item.account_delete_tx_hash,
        "last_known_rlusd_balance": _decimal_to_value(item.last_known_rlusd_balance),
        "last_known_xrp_balance_drops": item.last_known_xrp_balance_drops,
        "status": item.status,
        "last_error": item.last_error,
        "deleted_at": _serialize_datetime(item.deleted_at),
    }


def _deserialize_usdc_claim_wallet_state(payload: dict[str, Any]) -> USDCClaimWalletState:
    return USDCClaimWalletState(
        classic_address=str(payload["classic_address"]),
        seed=str(payload["seed"]),
        created_at=_deserialize_datetime(payload.get("created_at")) or datetime.now(timezone.utc),
        trustline_create_tx_hash=payload.get("trustline_create_tx_hash"),
        usdc_sweep_tx_hash=payload.get("usdc_sweep_tx_hash"),
        trustline_reset_tx_hash=payload.get("trustline_reset_tx_hash"),
        account_delete_tx_hash=payload.get("account_delete_tx_hash"),
        last_known_usdc_balance=Decimal(str(payload.get("last_known_usdc_balance", "0"))),
        last_known_xrp_balance_drops=(
            int(payload["last_known_xrp_balance_drops"])
            if payload.get("last_known_xrp_balance_drops") is not None
            else None
        ),
        status=str(payload.get("status", CLAIM_WALLET_STATUS_AWAITING_MANUAL_FUNDING)),
        last_error=payload.get("last_error"),
        deleted_at=_deserialize_datetime(payload.get("deleted_at")),
    )


def _serialize_usdc_claim_wallet_state(item: USDCClaimWalletState) -> dict[str, Any]:
    return {
        "classic_address": item.classic_address,
        "seed": item.seed,
        "created_at": _serialize_datetime(item.created_at),
        "trustline_create_tx_hash": item.trustline_create_tx_hash,
        "usdc_sweep_tx_hash": item.usdc_sweep_tx_hash,
        "trustline_reset_tx_hash": item.trustline_reset_tx_hash,
        "account_delete_tx_hash": item.account_delete_tx_hash,
        "last_known_usdc_balance": _decimal_to_value(item.last_known_usdc_balance),
        "last_known_xrp_balance_drops": item.last_known_xrp_balance_drops,
        "status": item.status,
        "last_error": item.last_error,
        "deleted_at": _serialize_datetime(item.deleted_at),
    }


def _format_usdc_manual_claim_message(address: str) -> str:
    return (
        f"Visit {CIRCLE_FAUCET_URL}, choose XRPL Testnet, and claim "
        f"{USDC_FAUCET_DRIP_AMOUNT} USDC to {address}. Then rerun "
        "`python -m devtools.usdc_topup` to sweep the funds into the accumulator wallet."
    )


def _load_cached_wallet_pair(cache_path: Path) -> LiveWalletPair | None:
    try:
        payload = json.loads(cache_path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None

    if payload.get("version") != WALLET_CACHE_VERSION:
        return None

    try:
        wallet_a = _wallet_from_cache_record(payload["wallet_a"])
        wallet_b = _wallet_from_cache_record(payload["wallet_b"])
    except (KeyError, TypeError, ValueError):
        return None

    return LiveWalletPair(wallet_a=wallet_a, wallet_b=wallet_b)


def _wallet_from_cache_record(record: dict[str, str]) -> Wallet:
    wallet = Wallet.from_seed(record["seed"])
    if wallet.classic_address != record["classic_address"]:
        raise ValueError("Wallet cache address mismatch")
    return wallet


def _write_wallet_cache(cache_path: Path, wallets: LiveWalletPair) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": WALLET_CACHE_VERSION,
        "wallet_a": _wallet_to_cache_record(wallets.wallet_a),
        "wallet_b": _wallet_to_cache_record(wallets.wallet_b),
    }
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        os.chmod(cache_path, 0o600)
    except OSError:
        pass


def _wallet_to_cache_record(wallet: Wallet) -> dict[str, str]:
    if wallet.seed is None:
        raise ValueError("Wallet seed missing")
    return {
        "seed": wallet.seed,
        "classic_address": wallet.classic_address,
    }


def _wallet_pair_is_active(client: JsonRpcClient, wallets: LiveWalletPair) -> bool:
    return all(_account_exists(client, wallet.classic_address) for wallet in wallets.as_list())


def _account_exists(client: JsonRpcClient, address: str) -> bool:
    return get_validated_account_root(client, address) is not None


def _response_tx_hash(response: dict[str, Any]) -> str:
    tx_payload = response.get("tx_json") or response.get("tx") or {}
    tx_hash = response.get("hash") or tx_payload.get("hash")
    if not tx_hash:
        raise RuntimeError("XRPL response did not include a transaction hash")
    return str(tx_hash)


def _assert_validated_success(response: dict[str, Any], operation_name: str) -> None:
    if response.get("validated") is not True:
        raise RuntimeError(f"{operation_name} did not validate")
    transaction_result = response.get("meta", {}).get("TransactionResult")
    if transaction_result != "tesSUCCESS":
        raise RuntimeError(f"{operation_name} failed with {transaction_result}")


def _mark_claim_wallet_deleted(claim_wallet: ClaimWalletState, now: datetime) -> bool:
    changed = False
    if claim_wallet.status != CLAIM_WALLET_STATUS_DELETED:
        claim_wallet.status = CLAIM_WALLET_STATUS_DELETED
        changed = True
    if claim_wallet.deleted_at != now:
        claim_wallet.deleted_at = now
        changed = True
    if claim_wallet.last_known_rlusd_balance != Decimal("0"):
        claim_wallet.last_known_rlusd_balance = Decimal("0")
        changed = True
    if claim_wallet.last_known_xrp_balance_drops != 0:
        claim_wallet.last_known_xrp_balance_drops = 0
        changed = True
    if claim_wallet.last_error is not None:
        claim_wallet.last_error = None
        changed = True
    return changed


def _mark_usdc_claim_wallet_deleted(claim_wallet: USDCClaimWalletState, now: datetime) -> bool:
    changed = False
    if claim_wallet.status != CLAIM_WALLET_STATUS_DELETED:
        claim_wallet.status = CLAIM_WALLET_STATUS_DELETED
        changed = True
    if claim_wallet.deleted_at != now:
        claim_wallet.deleted_at = now
        changed = True
    if claim_wallet.last_known_usdc_balance != Decimal("0"):
        claim_wallet.last_known_usdc_balance = Decimal("0")
        changed = True
    if claim_wallet.last_known_xrp_balance_drops != 0:
        claim_wallet.last_known_xrp_balance_drops = 0
        changed = True
    if claim_wallet.last_error is not None:
        claim_wallet.last_error = None
        changed = True
    return changed


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _deserialize_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    normalized_value = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal_to_value(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    if normalized == 0:
        return "0"
    return format(normalized.normalize(), "f")

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xrpl.clients import JsonRpcClient
from xrpl.core.binarycodec import encode
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import XRP
from xrpl.models.requests import AMMInfo, RipplePathFind, ServerState, Tx
from xrpl.models.transactions import Payment, PaymentFlag, TrustSet
from xrpl.transaction import autofill, sign, simulate, submit_and_wait
from xrpl.wallet import Wallet

from devtools.live_testnet_support import (
    DEFAULT_RLUSD_TESTNET_ISSUER,
    XRPL_TESTNET_RPC_URL_ENV,
    current_validated_ledger_index,
    get_demo_wallet_set,
    get_validated_account_root,
    get_validated_balance,
    get_validated_trustline_balance,
    resolve_live_testnet_rpc_url,
    trustline_limit_is_sufficient,
    wallet_cache_path,
)
from xrpl_x402_core import RLUSD_CODE, RLUSD_HEX, normalize_currency_code
from xrpl_x402_core.testnet_rpc import TESTNET_NETWORK_ID, probe_rpc_network_id

NETWORK = "xrpl:1"
DEFAULT_FUNDING_RPC_URL = "https://s.altnet.rippletest.net:51234/"
TESTNET_XRP_FAUCET_URL = "https://faucet.altnet.rippletest.net/accounts"
DEFAULT_WALLET_DIRECTORY = Path(".live-test-wallets")
DEFAULT_TARGET_RLUSD = Decimal("10")
DEFAULT_MAX_XRP = Decimal("35")
DEFAULT_SLIPPAGE_BPS = 500
DEFAULT_MAX_FEE_DROPS = 100
DEFAULT_TRUSTLINE_LIMIT = Decimal("100000")
DEFAULT_XRP_RESERVE_AND_FEE_BUFFER_DROPS = 5_000_000
FAUCET_RETRY_DELAY = timedelta(minutes=2)
FAUCET_VALIDATION_TIMEOUT_SECONDS = 45
TRANSACTION_STATE_VERSION = 1
WALLET_FILE_VERSION = 1
MAX_SLIPPAGE_BPS = 5_000


class FundingError(RuntimeError):
    """Base error for the browser-free RLUSD Testnet funding command."""


class LiquidityUnavailableError(FundingError):
    """Raised when Testnet has no usable XRP-to-RLUSD path."""


class SpendLimitExceededError(FundingError):
    """Raised before signing when the quoted XRP input exceeds the operator cap."""


class SettlementPendingError(FundingError):
    """Raised when a submitted transaction has not reached a terminal ledger state."""


@dataclass(frozen=True)
class RLUSDPathQuote:
    source_amount_drops: int
    paths: list[list[dict[str, Any]]]
    source: str


@dataclass
class PendingTransaction:
    purpose: str
    tx_hash: str
    signed_tx_blob: str
    first_ledger_sequence: int
    last_ledger_sequence: int
    created_at: str
    expected_rlusd_amount: str | None = None


@dataclass
class RLUSDFundingState:
    classic_address: str
    issuer: str
    network: str = NETWORK
    version: int = TRANSACTION_STATE_VERSION
    pending_transaction: PendingTransaction | None = None
    completed_transaction_hashes: list[str] = field(default_factory=list)
    last_faucet_attempt_at: str | None = None
    last_faucet_balance_drops: int | None = None


@dataclass(frozen=True)
class RLUSDFundingResult:
    status: str
    classic_address: str
    network: str
    issuer: str
    xrp_balance_drops: int
    rlusd_balance: Decimal
    target_rlusd: Decimal
    transaction_hashes: tuple[str, ...]
    state_path: Path
    wallet_path: Path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("amount must be finite")
    normalized = value.normalize()
    return format(normalized, "f")


def _xrp_to_drops(value: Decimal) -> int:
    drops = value * Decimal(1_000_000)
    if drops != drops.to_integral_value():
        raise ValueError("XRP amount must not use fractions smaller than one drop")
    return int(drops)


def compute_send_max_drops(source_amount: str | int, slippage_bps: int) -> int:
    """Round an XRP path quote up after applying the configured slippage bound."""

    try:
        drops = int(str(source_amount))
    except ValueError as exc:
        raise ValueError("source amount must be an integer drops value") from exc
    if drops <= 0:
        raise ValueError("source amount must be positive")
    if slippage_bps < 0 or slippage_bps > MAX_SLIPPAGE_BPS:
        raise ValueError(f"slippage basis points must be between 0 and {MAX_SLIPPAGE_BPS}")
    return (drops * (10_000 + slippage_bps) + 9_999) // 10_000


def select_path_quote(result: dict[str, Any]) -> RLUSDPathQuote:
    """Select the cheapest XRP-denominated path returned by ripple_path_find."""

    alternatives = result.get("alternatives")
    if not isinstance(alternatives, list):
        raise LiquidityUnavailableError("XRPL pathfinding returned no alternatives")

    candidates: list[RLUSDPathQuote] = []
    for alternative in alternatives:
        if not isinstance(alternative, dict):
            continue
        source_amount = alternative.get("source_amount")
        if not isinstance(source_amount, str) or not source_amount.isdigit():
            continue
        drops = int(source_amount)
        paths = alternative.get("paths_computed", [])
        if drops <= 0 or not isinstance(paths, list):
            continue
        if not all(
            isinstance(path, list) and all(isinstance(step, dict) for step in path)
            for path in paths
        ):
            continue
        candidates.append(
            RLUSDPathQuote(
                source_amount_drops=drops,
                paths=paths,
                source="path_find",
            )
        )

    if not candidates:
        raise LiquidityUnavailableError("XRPL pathfinding found no XRP-to-RLUSD route")
    return min(candidates, key=lambda quote: quote.source_amount_drops)


def _amm_source_amount_drops(result: dict[str, Any], amount: Decimal) -> int:
    amm = result.get("amm")
    if not isinstance(amm, dict):
        raise LiquidityUnavailableError("XRPL returned no XRP/RLUSD AMM")

    first = amm.get("amount")
    second = amm.get("amount2")
    if isinstance(first, str) and isinstance(second, dict):
        xrp_reserve = Decimal(first)
        rlusd_reserve = Decimal(str(second.get("value", "0")))
    elif isinstance(second, str) and isinstance(first, dict):
        xrp_reserve = Decimal(second)
        rlusd_reserve = Decimal(str(first.get("value", "0")))
    else:
        raise LiquidityUnavailableError("XRPL returned an unexpected XRP/RLUSD AMM shape")

    if xrp_reserve <= 0 or rlusd_reserve <= 0 or amount <= 0 or amount >= rlusd_reserve:
        raise LiquidityUnavailableError("XRP/RLUSD AMM does not have enough liquidity")

    trading_fee = Decimal(str(amm.get("trading_fee", 0))) / Decimal(100_000)
    if trading_fee < 0 or trading_fee >= 1:
        raise LiquidityUnavailableError("XRP/RLUSD AMM returned an invalid trading fee")
    input_drops = (
        (xrp_reserve * amount) / (rlusd_reserve - amount) / (Decimal(1) - trading_fee)
    )
    return int(input_drops.to_integral_value(rounding=ROUND_CEILING))


def build_rlusd_self_payment(
    address: str,
    issuer: str,
    amount: Decimal,
    send_max_drops: int,
    paths: list[list[dict[str, Any]]] | None = None,
) -> Payment:
    """Build an exact-output circular payment that converts XRP into RLUSD."""

    if issuer != DEFAULT_RLUSD_TESTNET_ISSUER:
        raise FundingError("refusing to acquire RLUSD from a non-official Testnet issuer")
    if amount <= 0 or send_max_drops <= 0:
        raise ValueError("payment amount and SendMax must be positive")
    # Do not copy server-provided paths into the signed transaction. The direct
    # XRP/RLUSD default path reaches both the DEX and AMM, while the exact
    # Amount, SendMax, and tfLimitQuality remain ledger-enforced bounds.
    del paths
    return Payment(
        account=address,
        destination=address,
        amount=IssuedCurrencyAmount(
            currency=RLUSD_HEX,
            issuer=issuer,
            value=_decimal_text(amount),
        ),
        send_max=str(send_max_drops),
        flags=int(PaymentFlag.TF_LIMIT_QUALITY),
    )


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise FundingError(f"refusing private state directory symlink {path}")
    if path.exists():
        if not path.is_dir():
            raise FundingError(f"private state parent is not a directory: {path}")
        if path.resolve() == DEFAULT_WALLET_DIRECTORY.resolve():
            os.chmod(path, 0o700)
        return
    path.mkdir(parents=True, mode=0o700)


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise FundingError(f"refusing to write private state through symlink {path}")


def atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a private JSON file without a world-readable creation window."""

    _private_directory(path.parent)
    _reject_symlink(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def private_file_lock(path: Path) -> Iterator[None]:
    _private_directory(path.parent)
    _reject_symlink(path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        # os.fdopen owns and closes descriptor on the normal path.
        pass


def write_funding_state(path: Path, state: RLUSDFundingState) -> None:
    payload = asdict(state)
    atomic_write_private_json(path, payload)


def load_funding_state(path: Path, address: str, issuer: str) -> RLUSDFundingState:
    _reject_symlink(path)
    if not path.exists():
        return RLUSDFundingState(classic_address=address, issuer=issuer)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FundingError(f"invalid RLUSD funding state at {path}") from exc
    if payload.get("version") != TRANSACTION_STATE_VERSION:
        raise FundingError(f"unsupported RLUSD funding state version at {path}")
    if payload.get("network") != NETWORK:
        raise FundingError(f"RLUSD funding state at {path} is not for {NETWORK}")
    if payload.get("classic_address") != address:
        raise FundingError(f"RLUSD funding state at {path} belongs to another wallet")
    if payload.get("issuer") != issuer:
        raise FundingError(f"RLUSD funding state at {path} uses another issuer")

    pending_payload = payload.get("pending_transaction")
    pending = None
    if pending_payload is not None:
        if not isinstance(pending_payload, dict):
            raise FundingError(f"invalid pending transaction in {path}")
        pending = PendingTransaction(**pending_payload)
    return RLUSDFundingState(
        classic_address=address,
        issuer=issuer,
        network=NETWORK,
        pending_transaction=pending,
        completed_transaction_hashes=[
            str(item) for item in payload.get("completed_transaction_hashes", [])
        ],
        last_faucet_attempt_at=payload.get("last_faucet_attempt_at"),
        last_faucet_balance_drops=payload.get("last_faucet_balance_drops"),
    )


def save_wallet_file(path: Path, wallet: Wallet, issuer: str) -> None:
    if wallet.seed is None:
        raise FundingError("generated Testnet wallet is missing its seed")
    atomic_write_private_json(
        path,
        {
            "version": WALLET_FILE_VERSION,
            "network": NETWORK,
            "classic_address": wallet.classic_address,
            "seed": wallet.seed,
            "rlusd_issuer": issuer,
            "created_at": _utc_now().isoformat(),
        },
    )


def load_wallet_file(path: Path, issuer: str) -> Wallet:
    _reject_symlink(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FundingError(f"wallet file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FundingError(f"invalid wallet file: {path}") from exc
    if payload.get("version") != WALLET_FILE_VERSION or payload.get("network") != NETWORK:
        raise FundingError(f"wallet file is not a supported {NETWORK} wallet: {path}")
    if payload.get("rlusd_issuer") != issuer:
        raise FundingError(f"wallet file uses a different RLUSD issuer: {path}")
    try:
        wallet = Wallet.from_seed(str(payload["seed"]))
    except (KeyError, ValueError) as exc:
        raise FundingError(f"wallet file has an invalid seed: {path}") from exc
    if wallet.classic_address != payload.get("classic_address"):
        raise FundingError(f"wallet address does not match its seed: {path}")
    return wallet


def create_test_wallet() -> Wallet:
    return Wallet.create()


def request_testnet_xrp(address: str) -> None:
    request = Request(
        TESTNET_XRP_FAUCET_URL,
        data=json.dumps({"destination": address}).encode("utf-8"),
        headers={"content-type": "application/json", "user-agent": "xrpl-x402-stack/0.2.0"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise FundingError(f"XRP Testnet faucet returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise FundingError(f"XRP Testnet faucet failed: {exc.reason or exc}") from exc


def _safe_xrp_balance(client: JsonRpcClient, address: str) -> int:
    try:
        return get_validated_balance(client, address)
    except Exception:
        return 0


def _wait_for_xrp_balance(
    client: JsonRpcClient,
    address: str,
    minimum_drops: int,
    *,
    timeout_seconds: int = FAUCET_VALIDATION_TIMEOUT_SECONDS,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    balance = _safe_xrp_balance(client, address)
    while balance < minimum_drops and time.monotonic() < deadline:
        time.sleep(1)
        balance = _safe_xrp_balance(client, address)
    return balance


def ensure_testnet_xrp(
    client: JsonRpcClient,
    address: str,
    minimum_drops: int,
    state: RLUSDFundingState,
    state_path: Path,
) -> int:
    balance = _safe_xrp_balance(client, address)
    if balance >= minimum_drops:
        return balance

    now = _utc_now()
    if state.last_faucet_attempt_at:
        previous = datetime.fromisoformat(state.last_faucet_attempt_at.replace("Z", "+00:00"))
        if now < previous + FAUCET_RETRY_DELAY:
            raise SettlementPendingError(
                "the previous XRP faucet request is still in its reconciliation window"
            )

    state.last_faucet_attempt_at = now.isoformat()
    state.last_faucet_balance_drops = balance
    write_funding_state(state_path, state)
    try:
        request_testnet_xrp(address)
    except Exception as exc:
        reconciled = _wait_for_xrp_balance(client, address, minimum_drops)
        if reconciled >= minimum_drops:
            return reconciled
        raise SettlementPendingError(
            "XRP faucet outcome is uncertain; rerun after the reconciliation window"
        ) from exc

    funded = _wait_for_xrp_balance(client, address, minimum_drops)
    if funded < minimum_drops:
        raise SettlementPendingError(
            f"XRP faucet funding is not validated yet; have {funded} drops, need {minimum_drops}"
        )
    return funded


def quote_rlusd_path(
    client: JsonRpcClient,
    address: str,
    issuer: str,
    amount: Decimal,
) -> RLUSDPathQuote:
    destination_amount = IssuedCurrencyAmount(
        currency=RLUSD_HEX,
        issuer=issuer,
        value=_decimal_text(amount),
    )
    path_error: Exception | None = None
    try:
        response = client.request(
            RipplePathFind(
                source_account=address,
                destination_account=address,
                destination_amount=destination_amount,
                source_currencies=[XRP()],
                ledger_index="validated",
            )
        )
        if not response.is_successful():
            raise LiquidityUnavailableError("XRPL pathfinding request failed")
        result = response.result
        if result.get("validated") is not True:
            raise FundingError("XRPL path quote was not based on a validated ledger")
        if result.get("source_account") != address:
            raise FundingError("XRPL path quote source account mismatch")
        if result.get("destination_account") != address:
            raise FundingError("XRPL path quote destination account mismatch")
        quoted_destination = result.get("destination_amount")
        if not isinstance(quoted_destination, dict):
            raise FundingError("XRPL path quote omitted the destination amount")
        try:
            quoted_currency = normalize_currency_code(
                str(quoted_destination.get("currency", ""))
            )
            quoted_value = Decimal(str(quoted_destination.get("value", "")))
        except (ValueError, InvalidOperation) as exc:
            raise FundingError("XRPL path quote returned an invalid destination amount") from exc
        if (
            quoted_currency != RLUSD_CODE
            or quoted_destination.get("issuer") != issuer
            or quoted_value != amount
        ):
            raise FundingError("XRPL path quote destination amount mismatch")
        return select_path_quote(result)
    except LiquidityUnavailableError as exc:
        path_error = exc
    except FundingError:
        raise
    except Exception as exc:
        path_error = exc

    try:
        response = client.request(
            AMMInfo(
                asset=XRP(),
                asset2=destination_amount.to_currency(),
            )
        )
        return RLUSDPathQuote(
            source_amount_drops=_amm_source_amount_drops(response.result, amount),
            paths=[],
            source="amm",
        )
    except Exception as amm_error:
        raise LiquidityUnavailableError(
            f"no usable XRP-to-RLUSD Testnet route: pathfinding={path_error}; AMM={amm_error}"
        ) from amm_error


def _transaction_result(response_result: dict[str, Any]) -> str | None:
    meta = response_result.get("meta") or response_result.get("metaData")
    if isinstance(meta, dict):
        result = meta.get("TransactionResult")
        if result is not None:
            return str(result)
    engine_result = response_result.get("engine_result")
    return str(engine_result) if engine_result is not None else None


def _exact_delivered_amount(
    response_result: dict[str, Any],
    issuer: str,
    expected: Decimal,
) -> None:
    meta = response_result.get("meta") or response_result.get("metaData")
    if not isinstance(meta, dict):
        raise FundingError("validated conversion omitted transaction metadata")
    delivered = meta.get("delivered_amount") or meta.get("DeliveredAmount")
    if not isinstance(delivered, dict):
        raise FundingError("validated conversion omitted delivered_amount")
    try:
        currency = normalize_currency_code(str(delivered.get("currency", "")))
        value = Decimal(str(delivered.get("value", "")))
    except (ValueError, InvalidOperation) as exc:
        raise FundingError("validated conversion returned an invalid delivered_amount") from exc
    if currency != RLUSD_CODE or delivered.get("issuer") != issuer or value != expected:
        raise FundingError("validated conversion did not deliver the exact expected RLUSD")


def _validated_success(
    response_result: dict[str, Any],
    purpose: str,
    *,
    issuer: str = DEFAULT_RLUSD_TESTNET_ISSUER,
    expected_rlusd_amount: Decimal | None = None,
) -> None:
    if response_result.get("validated") is not True:
        raise SettlementPendingError(f"{purpose} has not validated")
    result = _transaction_result(response_result)
    if result != "tesSUCCESS":
        raise FundingError(f"{purpose} failed with {result or 'unknown result'}")
    if expected_rlusd_amount is not None:
        _exact_delivered_amount(response_result, issuer, expected_rlusd_amount)


def _record_terminal_transaction(
    state: RLUSDFundingState,
    state_path: Path,
    tx_hash: str,
) -> None:
    if tx_hash not in state.completed_transaction_hashes:
        state.completed_transaction_hashes.append(tx_hash)
    state.pending_transaction = None
    write_funding_state(state_path, state)


def reconcile_pending_transaction(
    client: JsonRpcClient,
    state: RLUSDFundingState,
    state_path: Path,
) -> bool:
    """Reconcile or safely rebroadcast the identical signed transaction.

    Returns true for a validated success and false only when the transaction is
    confirmed absent after LastLedgerSequence. It raises while the outcome is
    still indeterminate or when a validated transaction failed.
    """

    pending = state.pending_transaction
    if pending is None:
        return True

    response = client.request(
        Tx(
            transaction=pending.tx_hash,
            min_ledger=pending.first_ledger_sequence,
            max_ledger=pending.last_ledger_sequence,
        )
    )
    result = response.result
    if result.get("validated") is True:
        try:
            _validated_success(
                result,
                pending.purpose,
                issuer=state.issuer,
                expected_rlusd_amount=(
                    Decimal(pending.expected_rlusd_amount)
                    if pending.expected_rlusd_amount is not None
                    else None
                ),
            )
        except FundingError:
            state.pending_transaction = None
            write_funding_state(state_path, state)
            raise
        _record_terminal_transaction(state, state_path, pending.tx_hash)
        return True

    current_ledger = current_validated_ledger_index(client)
    if current_ledger >= pending.last_ledger_sequence:
        if (
            result.get("error") == "txnNotFound"
            and result.get("searched_all") is True
        ):
            state.pending_transaction = None
            write_funding_state(state_path, state)
            return False
        raise SettlementPendingError(
            f"{pending.purpose} {pending.tx_hash} is past LastLedgerSequence, "
            "but its ledger outcome is not authoritative"
        )

    try:
        submitted = submit_and_wait(
            pending.signed_tx_blob,
            client,
            check_fee=False,
            autofill=False,
        )
        _validated_success(
            submitted.result,
            pending.purpose,
            issuer=state.issuer,
            expected_rlusd_amount=(
                Decimal(pending.expected_rlusd_amount)
                if pending.expected_rlusd_amount is not None
                else None
            ),
        )
    except SettlementPendingError:
        raise
    except FundingError:
        state.pending_transaction = None
        write_funding_state(state_path, state)
        raise
    except Exception as exc:
        raise SettlementPendingError(
            f"{pending.purpose} {pending.tx_hash} remains pending"
        ) from exc

    _record_terminal_transaction(state, state_path, pending.tx_hash)
    return True


def submit_journaled_transaction(
    client: JsonRpcClient,
    wallet: Wallet,
    transaction: Payment | TrustSet,
    *,
    purpose: str,
    max_fee_drops: int,
    state: RLUSDFundingState,
    state_path: Path,
    expected_rlusd_amount: Decimal | None = None,
) -> str:
    if state.pending_transaction is not None:
        raise SettlementPendingError("another transaction must be reconciled first")

    first_ledger_sequence = current_validated_ledger_index(client)
    prepared = autofill(transaction, client)
    fee = int(str(prepared.fee or "0"))
    if fee <= 0 or fee > max_fee_drops:
        raise SpendLimitExceededError(
            f"refusing {purpose}: fee {fee} drops exceeds cap {max_fee_drops}"
        )
    if prepared.last_ledger_sequence is None:
        raise FundingError(f"prepared {purpose} is missing LastLedgerSequence")
    if isinstance(prepared, Payment):
        _simulate_exact_payment(
            client,
            prepared,
            issuer=state.issuer,
            expected_rlusd_amount=expected_rlusd_amount,
        )
    signed = sign(prepared, wallet)

    tx_hash = signed.get_hash()
    state.pending_transaction = PendingTransaction(
        purpose=purpose,
        tx_hash=tx_hash,
        signed_tx_blob=encode(signed.to_xrpl()),
        first_ledger_sequence=first_ledger_sequence,
        last_ledger_sequence=int(signed.last_ledger_sequence),
        created_at=_utc_now().isoformat(),
        expected_rlusd_amount=(
            _decimal_text(expected_rlusd_amount)
            if expected_rlusd_amount is not None
            else None
        ),
    )
    write_funding_state(state_path, state)

    try:
        response = submit_and_wait(
            state.pending_transaction.signed_tx_blob,
            client,
            check_fee=False,
            autofill=False,
        )
        _validated_success(
            response.result,
            purpose,
            issuer=state.issuer,
            expected_rlusd_amount=expected_rlusd_amount,
        )
    except SettlementPendingError:
        raise
    except FundingError:
        state.pending_transaction = None
        write_funding_state(state_path, state)
        raise
    except Exception as exc:
        raise SettlementPendingError(
            f"{purpose} {tx_hash} has an uncertain submission outcome"
        ) from exc
    _record_terminal_transaction(state, state_path, tx_hash)
    return tx_hash


def _ensure_rlusd_trustline_journaled(
    client: JsonRpcClient,
    wallet: Wallet,
    issuer: str,
    *,
    max_fee_drops: int,
    state: RLUSDFundingState,
    state_path: Path,
) -> str | None:
    if trustline_limit_is_sufficient(
        client,
        wallet.classic_address,
        issuer,
        DEFAULT_TRUSTLINE_LIMIT,
    ):
        return None
    transaction = TrustSet(
        account=wallet.classic_address,
        flags=262144,
        limit_amount={
            "currency": RLUSD_HEX,
            "issuer": issuer,
            "value": _decimal_text(DEFAULT_TRUSTLINE_LIMIT),
        },
    )
    return submit_journaled_transaction(
        client,
        wallet,
        transaction,
        purpose="RLUSD trustline",
        max_fee_drops=max_fee_drops,
        state=state,
        state_path=state_path,
    )


def _simulate_exact_payment(
    client: JsonRpcClient,
    transaction: Payment,
    *,
    issuer: str,
    expected_rlusd_amount: Decimal | None,
) -> None:
    try:
        response = simulate(transaction, client)
    except Exception as exc:
        # Some public Testnet servers do not expose simulate. SendMax and the
        # absence of tfPartialPayment remain hard ledger-enforced limits.
        error_text = str(exc).lower()
        if "notimpl" in error_text or "not implemented" in error_text or "unknown method" in error_text:
            return
        raise FundingError(f"RLUSD conversion simulation request failed: {exc}") from exc
    result = _transaction_result(response.result)
    if result != "tesSUCCESS":
        raise FundingError(f"RLUSD conversion simulation failed with {result or 'unknown result'}")
    if expected_rlusd_amount is not None:
        _exact_delivered_amount(response.result, issuer, expected_rlusd_amount)


def _assert_spendable_xrp(
    client: JsonRpcClient,
    address: str,
    *,
    send_max_drops: int,
    max_fee_drops: int,
) -> None:
    account = get_validated_account_root(client, address)
    if account is None:
        raise FundingError(f"XRPL Testnet account does not exist: {address}")
    account_data = account["account_data"]
    response = client.request(ServerState())
    if not response.is_successful():
        raise FundingError("could not read XRPL Testnet reserve settings")
    state = response.result.get("state")
    if not isinstance(state, dict) or int(str(state.get("network_id", -1))) != TESTNET_NETWORK_ID:
        raise FundingError("reserve response is not from XRPL Testnet")
    validated = state.get("validated_ledger")
    if not isinstance(validated, dict):
        raise FundingError("XRPL Testnet reserve response omitted validated_ledger")
    try:
        reserve_base = int(str(validated["reserve_base"]))
        reserve_inc = int(str(validated["reserve_inc"]))
        owner_count = int(str(account_data.get("OwnerCount", 0)))
        balance = int(str(account_data["Balance"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise FundingError("XRPL Testnet returned invalid reserve or balance data") from exc
    required = (
        reserve_base
        + reserve_inc * owner_count
        + DEFAULT_XRP_RESERVE_AND_FEE_BUFFER_DROPS
        + send_max_drops
        + max_fee_drops
    )
    if balance < required:
        raise SpendLimitExceededError(
            f"insufficient spendable XRP: have {balance} drops, require {required} after reserves"
        )


def fund_rlusd_wallet(
    client: JsonRpcClient,
    wallet: Wallet,
    *,
    state_path: Path,
    wallet_path: Path,
    issuer: str = DEFAULT_RLUSD_TESTNET_ISSUER,
    target_rlusd: Decimal = DEFAULT_TARGET_RLUSD,
    max_xrp_drops: int = 35_000_000,
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
    max_fee_drops: int = DEFAULT_MAX_FEE_DROPS,
) -> RLUSDFundingResult:
    if issuer != DEFAULT_RLUSD_TESTNET_ISSUER:
        raise FundingError("refusing a non-official RLUSD Testnet issuer")
    if target_rlusd <= 0:
        raise ValueError("target RLUSD balance must be positive")
    if max_xrp_drops <= 0 or max_fee_drops <= 0:
        raise ValueError("XRP and fee caps must be positive")
    compute_send_max_drops(1, slippage_bps)

    state = load_funding_state(state_path, wallet.classic_address, issuer)
    if state.pending_transaction is not None:
        reconcile_pending_transaction(client, state, state_path)

    try:
        starting_rlusd = get_validated_trustline_balance(
            client,
            wallet.classic_address,
            issuer,
        )
    except Exception:
        starting_rlusd = Decimal("0")

    if starting_rlusd >= target_rlusd:
        return RLUSDFundingResult(
            status="ready",
            classic_address=wallet.classic_address,
            network=NETWORK,
            issuer=issuer,
            xrp_balance_drops=_safe_xrp_balance(client, wallet.classic_address),
            rlusd_balance=starting_rlusd,
            target_rlusd=target_rlusd,
            transaction_hashes=tuple(state.completed_transaction_hashes),
            state_path=state_path,
            wallet_path=wallet_path,
        )

    minimum_xrp = max_xrp_drops + DEFAULT_XRP_RESERVE_AND_FEE_BUFFER_DROPS
    ensure_testnet_xrp(client, wallet.classic_address, minimum_xrp, state, state_path)
    _ensure_rlusd_trustline_journaled(
        client,
        wallet,
        issuer,
        max_fee_drops=max_fee_drops,
        state=state,
        state_path=state_path,
    )

    current_rlusd = get_validated_trustline_balance(client, wallet.classic_address, issuer)
    if current_rlusd < target_rlusd:
        needed = target_rlusd - current_rlusd
        quote = quote_rlusd_path(client, wallet.classic_address, issuer, needed)
        send_max_drops = compute_send_max_drops(quote.source_amount_drops, slippage_bps)
        if send_max_drops > max_xrp_drops:
            raise SpendLimitExceededError(
                f"quoted SendMax {send_max_drops} drops exceeds --max-xrp cap {max_xrp_drops}"
            )
        payment = build_rlusd_self_payment(
            wallet.classic_address,
            issuer,
            needed,
            send_max_drops,
            quote.paths,
        )
        _assert_spendable_xrp(
            client,
            wallet.classic_address,
            send_max_drops=send_max_drops,
            max_fee_drops=max_fee_drops,
        )
        submit_journaled_transaction(
            client,
            wallet,
            payment,
            purpose="XRP-to-RLUSD conversion",
            max_fee_drops=max_fee_drops,
            state=state,
            state_path=state_path,
            expected_rlusd_amount=needed,
        )

    final_rlusd = get_validated_trustline_balance(client, wallet.classic_address, issuer)
    if final_rlusd < target_rlusd:
        raise FundingError(
            f"conversion validated but RLUSD balance is {final_rlusd}, below target {target_rlusd}"
        )
    return RLUSDFundingResult(
        status="funded" if final_rlusd > starting_rlusd else "ready",
        classic_address=wallet.classic_address,
        network=NETWORK,
        issuer=issuer,
        xrp_balance_drops=get_validated_balance(client, wallet.classic_address),
        rlusd_balance=final_rlusd,
        target_rlusd=target_rlusd,
        transaction_hashes=tuple(state.completed_transaction_hashes),
        state_path=state_path,
        wallet_path=wallet_path,
    )


def _positive_decimal(raw: str, label: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a decimal number") from exc
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"{label} must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or reuse an XRPL Testnet wallet, faucet-fund XRP, and acquire "
            "official Testnet RLUSD through an exact-output on-ledger conversion."
        )
    )
    parser.add_argument(
        "--new-wallet",
        action="store_true",
        help="Create a new private wallet file instead of using the cached RLUSD demo buyer",
    )
    parser.add_argument(
        "--wallet-file",
        type=Path,
        default=None,
        help="Private wallet file to create with --new-wallet or resume without it",
    )
    parser.add_argument(
        "--target-rlusd",
        type=lambda value: _positive_decimal(value, "target RLUSD"),
        default=DEFAULT_TARGET_RLUSD,
        help="Minimum final RLUSD balance (default: 10)",
    )
    parser.add_argument(
        "--max-xrp",
        type=lambda value: _positive_decimal(value, "maximum XRP"),
        default=DEFAULT_MAX_XRP,
        help="Maximum XRP SendMax for this conversion (default: 35)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=int,
        default=DEFAULT_SLIPPAGE_BPS,
        help="Slippage added to the path quote, in basis points (default: 500)",
    )
    parser.add_argument(
        "--max-fee-drops",
        type=int,
        default=DEFAULT_MAX_FEE_DROPS,
        help="Maximum fee for each signed transaction (default: 100 drops)",
    )
    parser.add_argument(
        "--xrpl-rpc-url",
        default=None,
        help="Optional XRPL Testnet JSON-RPC endpoint override",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def _timestamped_wallet_path() -> Path:
    timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_WALLET_DIRECTORY / f"rlusd-funded-wallet-{timestamp}.json"


def _state_path_for_wallet(wallet_path: Path) -> Path:
    return wallet_path.with_name(f"{wallet_path.stem}.funding.json")


def _select_wallet(
    client: JsonRpcClient,
    *,
    new_wallet: bool,
    wallet_file: Path | None,
    issuer: str,
) -> tuple[Wallet, Path, Path]:
    if new_wallet:
        resolved_path = wallet_file or _timestamped_wallet_path()
        if resolved_path.exists() or resolved_path.is_symlink():
            raise FundingError(f"refusing to overwrite existing wallet file: {resolved_path}")
        wallet = create_test_wallet()
        save_wallet_file(resolved_path, wallet, issuer)
        return wallet, resolved_path, _state_path_for_wallet(resolved_path)
    if wallet_file is not None:
        wallet = load_wallet_file(wallet_file, issuer)
        return wallet, wallet_file, _state_path_for_wallet(wallet_file)

    wallet = get_demo_wallet_set(client).buyer_wallet("rlusd")
    cache_path = wallet_cache_path()
    state_path = cache_path.parent / "rlusd-demo-buyer.funding.json"
    return wallet, cache_path, state_path


def _result_payload(result: RLUSDFundingResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "network": result.network,
        "address": result.classic_address,
        "issuer": result.issuer,
        "xrpBalanceDrops": result.xrp_balance_drops,
        "rlusdBalance": _decimal_text(result.rlusd_balance),
        "targetRLUSD": _decimal_text(result.target_rlusd),
        "transactionHashes": list(result.transaction_hashes),
        "walletFile": str(result.wallet_path),
        "stateFile": str(result.state_path),
    }


def _print_result(result: RLUSDFundingResult, *, as_json: bool) -> None:
    payload = _result_payload(result)
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"Status: {payload['status']}")
    print(f"Address: {payload['address']}")
    print(f"XRP balance: {Decimal(result.xrp_balance_drops) / Decimal(1_000_000)}")
    print(f"RLUSD balance: {payload['rlusdBalance']}")
    print(f"Issuer: {payload['issuer']}")
    print(f"Wallet file: {payload['walletFile']}")
    print(f"State file: {payload['stateFile']}")
    for tx_hash in result.transaction_hashes:
        print(f"Transaction: {tx_hash}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    lock_target_path: Path | None = None
    resume_wallet_path: Path | None = None
    selected_address: str | None = None
    try:
        if args.slippage_bps < 0 or args.slippage_bps > MAX_SLIPPAGE_BPS:
            raise FundingError(
                f"--slippage-bps must be between 0 and {MAX_SLIPPAGE_BPS}"
            )
        if args.max_fee_drops <= 0:
            raise FundingError("--max-fee-drops must be positive")
        # The XRPL Labs endpoint can answer a health probe and then require a
        # custom connectivity agreement for transaction methods. Use Ripple's
        # public Testnet endpoint for this write-oriented tool by default.
        rpc_url = resolve_live_testnet_rpc_url(
            args.xrpl_rpc_url
            or os.environ.get(XRPL_TESTNET_RPC_URL_ENV)
            or DEFAULT_FUNDING_RPC_URL
        )
        network_id = probe_rpc_network_id(rpc_url)
        if network_id != TESTNET_NETWORK_ID:
            raise FundingError(
                f"refusing XRPL network id {network_id}; this command is Testnet-only"
            )
        client = JsonRpcClient(rpc_url)
        issuer = DEFAULT_RLUSD_TESTNET_ISSUER

        lock_target_path = args.wallet_file or (
            _timestamped_wallet_path() if args.new_wallet else wallet_cache_path()
        )
        if args.new_wallet or args.wallet_file is not None:
            resume_wallet_path = lock_target_path
        lock_path = lock_target_path.with_name(f".{lock_target_path.name}.lock")
        with private_file_lock(lock_path):
            wallet, wallet_path, state_path = _select_wallet(
                client,
                new_wallet=args.new_wallet,
                wallet_file=(lock_target_path if args.new_wallet else args.wallet_file),
                issuer=issuer,
            )
            selected_address = wallet.classic_address
            result = fund_rlusd_wallet(
                client,
                wallet,
                state_path=state_path,
                wallet_path=wallet_path,
                issuer=issuer,
                target_rlusd=args.target_rlusd,
                max_xrp_drops=_xrp_to_drops(args.max_xrp),
                slippage_bps=args.slippage_bps,
                max_fee_drops=args.max_fee_drops,
            )
        _print_result(result, as_json=args.json)
        return 0
    except SettlementPendingError as exc:
        payload: dict[str, Any] = {"status": "pending", "error": str(exc)}
        if resume_wallet_path is not None:
            payload["walletFile"] = str(resume_wallet_path)
        else:
            payload["resumeHint"] = "rerun the same command without --wallet-file"
        if selected_address is not None:
            payload["address"] = selected_address
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"RLUSD funding pending: {exc}")
            if resume_wallet_path is not None:
                print(f"Resume wallet file: {resume_wallet_path}")
            else:
                print("Resume: rerun the same command without --wallet-file")
        return 3
    except (LiquidityUnavailableError, SpendLimitExceededError) as exc:
        if args.json:
            print(json.dumps({"status": "refused", "error": str(exc)}, sort_keys=True))
        else:
            print(f"RLUSD funding refused: {exc}")
        return 2
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True))
        else:
            print(f"RLUSD funding failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

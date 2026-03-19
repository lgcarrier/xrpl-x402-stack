from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import Payment
from xrpl.transaction import autofill, submit_and_wait
from xrpl.wallet import Wallet

from devtools.live_testnet_support import (
    ensure_rlusd_trustline,
    ensure_usdc_trustline,
    get_validated_balance,
    get_validated_trustline_balance,
    get_validated_usdc_trustline_balance,
    load_cached_demo_wallet_set,
    submit_validated_rlusd_payment,
    submit_validated_usdc_payment,
    wait_for_trustline_balance_increase,
    wallet_cache_path,
)
from xrpl_x402_core import RLUSD_CODE, USDC_CODE

DEFAULT_CONTRACT_PATH = Path("demo.contract.json")
DEFAULT_MERCHANT_XRP_FLOOR_XRP = Decimal("100")
XRP_DROPS_PER_XRP = Decimal("1000000")
XRP_REBALANCE_FEE_CUSHION_DROPS = 50


@dataclass(frozen=True)
class ContractAsset:
    symbol: str
    env_path: Path


@dataclass(frozen=True)
class WalletBalances:
    xrp_drops: int
    rlusd_balance: Decimal
    usdc_balance: Decimal


@dataclass(frozen=True)
class RebalanceResult:
    symbol: str
    env_path: Path
    merchant_address: str
    buyer_address: str
    merchant_balances: WalletBalances
    buyer_balances: WalletBalances
    status: str
    moved_amount: Decimal
    tx_hash: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rebalance demo funds back into the contract-referenced buyer wallets "
            "after a series of `demo.run.sh` executions."
        ),
    )
    parser.add_argument(
        "--contract",
        default=str(DEFAULT_CONTRACT_PATH),
        help="Path to the demo contract JSON file",
    )
    parser.add_argument(
        "--wallet-cache",
        default=str(wallet_cache_path()),
        help="Path to the cached demo wallet JSON file",
    )
    parser.add_argument(
        "--rebalance-xrp",
        action="store_true",
        help="Also move merchant XRP above the configured post-fee floor back to the XRP buyer wallet",
    )
    parser.add_argument(
        "--merchant-xrp-floor",
        default=str(DEFAULT_MERCHANT_XRP_FLOOR_XRP),
        help="Post-fee XRP floor to keep on the merchant wallet when --rebalance-xrp is enabled",
    )
    return parser


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def load_contract_assets(contract_path: Path) -> list[ContractAsset]:
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    contract_dir = contract_path.parent
    execution_envs = payload.get("execution", {}).get("env_files", {})
    assets: list[ContractAsset] = []

    for item in payload.get("assets", []):
        symbol = str(item["symbol"]).upper()
        env_spec = item.get("env") or execution_envs.get(symbol)
        if not env_spec:
            raise RuntimeError(f"Contract asset {symbol} is missing an env file mapping")
        env_path = Path(env_spec)
        if not env_path.is_absolute():
            env_path = contract_dir / env_path
        assets.append(ContractAsset(symbol=symbol, env_path=env_path.resolve()))

    return assets


def parse_xrp_to_drops(value: Decimal | str) -> int:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((decimal_value * XRP_DROPS_PER_XRP).to_integral_value())


def format_amount(symbol: str, amount: Decimal) -> str:
    if symbol == "XRP":
        return f"{(amount / XRP_DROPS_PER_XRP).quantize(Decimal('0.000001'))} XRP"
    return f"{format_decimal(amount)} {symbol}"


def format_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def capture_wallet_balances(
    client: JsonRpcClient,
    *,
    address: str,
    rlusd_issuer: str | None,
    usdc_issuer: str | None,
) -> WalletBalances:
    rlusd_balance = Decimal("0")
    if rlusd_issuer:
        rlusd_balance = get_validated_trustline_balance(
            client,
            address,
            rlusd_issuer,
            currency_code=RLUSD_CODE,
        )

    usdc_balance = Decimal("0")
    if usdc_issuer:
        usdc_balance = get_validated_usdc_trustline_balance(
            client,
            address,
            usdc_issuer,
        )

    return WalletBalances(
        xrp_drops=get_validated_balance(client, address),
        rlusd_balance=rlusd_balance,
        usdc_balance=usdc_balance,
    )


def format_wallet_balances(balances: WalletBalances) -> str:
    return ", ".join(
        [
            f"XRP {format_amount('XRP', Decimal(balances.xrp_drops))}",
            f"RLUSD {format_amount('RLUSD', balances.rlusd_balance)}",
            f"USDC {format_amount('USDC', balances.usdc_balance)}",
        ]
    )


def wait_for_xrp_balance_increase(
    client: JsonRpcClient,
    address: str,
    *,
    starting_balance: int,
    increase: int,
    timeout_seconds: int = 30,
) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        balance = get_validated_balance(client, address)
        if balance >= starting_balance + increase:
            return balance
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for XRP balance increase of {increase} on {address}")


def submit_validated_xrp_payment(
    client: JsonRpcClient,
    wallet: Wallet,
    destination_address: str,
    amount_drops: int,
) -> str:
    payment = Payment(
        account=wallet.classic_address,
        destination=destination_address,
        amount=str(amount_drops),
    )
    response = submit_and_wait(payment, client, wallet).result
    if response.get("validated") is not True:
        raise RuntimeError("XRP transfer did not validate")
    transaction_result = response.get("meta", {}).get("TransactionResult")
    if transaction_result != "tesSUCCESS":
        raise RuntimeError(f"XRP transfer failed with {transaction_result}")
    tx_payload = response.get("tx_json") or response.get("tx") or {}
    tx_hash = response.get("hash") or tx_payload.get("hash")
    if not tx_hash:
        raise RuntimeError("XRP transfer response did not include a transaction hash")
    return str(tx_hash)


def estimate_xrp_payment_fee_drops(
    client: JsonRpcClient,
    wallet: Wallet,
    destination_address: str,
) -> int:
    try:
        payment = Payment(
            account=wallet.classic_address,
            destination=destination_address,
            amount="1",
        )
        autofilled_payment = autofill(payment, client)
        fee_drops = autofilled_payment.fee
        if fee_drops is None:
            raise ValueError("missing fee")
        return int(str(fee_drops))
    except Exception as exc:
        raise RuntimeError(f"Unable to estimate XRP transfer fee: {exc}") from exc


def validate_cached_merchant(env_path: Path, env_values: dict[str, str], cached_merchant: Wallet) -> None:
    expected_address = (env_values.get("MY_DESTINATION_ADDRESS") or "").strip()
    if not expected_address:
        raise RuntimeError(f"{env_path} is missing MY_DESTINATION_ADDRESS")
    if expected_address != cached_merchant.classic_address:
        raise RuntimeError(
            f"{env_path} points at merchant {expected_address}, "
            f"but the cached merchant wallet is {cached_merchant.classic_address}"
        )


def validate_cached_buyer(
    env_path: Path,
    env_values: dict[str, str],
    *,
    asset_symbol: str,
    cached_buyer: Wallet,
) -> None:
    buyer_seed = (env_values.get("XRPL_WALLET_SEED") or "").strip()
    if not buyer_seed:
        raise RuntimeError(f"{env_path} is missing XRPL_WALLET_SEED")

    env_buyer = Wallet.from_seed(buyer_seed)
    if env_buyer.classic_address != cached_buyer.classic_address:
        raise RuntimeError(
            f"{env_path} points at {asset_symbol} buyer {env_buyer.classic_address}, "
            f"but the cached buyer wallet is {cached_buyer.classic_address}. "
            "Regenerate the env file from the current quickstart wallet cache before rebalancing."
        )


def rebalance_rlusd_asset(
    client: JsonRpcClient,
    *,
    merchant_wallet: Wallet,
    buyer_wallet: Wallet,
    issuer: str,
) -> tuple[Decimal, str | None]:
    ensure_rlusd_trustline(client, buyer_wallet, issuer)
    merchant_balance = get_validated_trustline_balance(
        client,
        merchant_wallet.classic_address,
        issuer,
        currency_code=RLUSD_CODE,
    )
    if merchant_balance <= 0:
        return Decimal("0"), None

    buyer_starting_balance = get_validated_trustline_balance(
        client,
        buyer_wallet.classic_address,
        issuer,
        currency_code=RLUSD_CODE,
    )
    tx_hash = submit_validated_rlusd_payment(
        client,
        merchant_wallet,
        buyer_wallet.classic_address,
        issuer,
        merchant_balance,
    )
    wait_for_trustline_balance_increase(
        client,
        buyer_wallet.classic_address,
        issuer,
        starting_balance=buyer_starting_balance,
        increase=merchant_balance,
        currency_code=RLUSD_CODE,
    )
    return merchant_balance, tx_hash


def rebalance_usdc_asset(
    client: JsonRpcClient,
    *,
    merchant_wallet: Wallet,
    buyer_wallet: Wallet,
    issuer: str,
) -> tuple[Decimal, str | None]:
    ensure_usdc_trustline(client, buyer_wallet, issuer)
    merchant_balance = get_validated_usdc_trustline_balance(
        client,
        merchant_wallet.classic_address,
        issuer,
    )
    if merchant_balance <= 0:
        return Decimal("0"), None

    buyer_starting_balance = get_validated_usdc_trustline_balance(
        client,
        buyer_wallet.classic_address,
        issuer,
    )
    tx_hash = submit_validated_usdc_payment(
        client,
        merchant_wallet,
        buyer_wallet.classic_address,
        issuer,
        merchant_balance,
    )
    wait_for_trustline_balance_increase(
        client,
        buyer_wallet.classic_address,
        issuer,
        starting_balance=buyer_starting_balance,
        increase=merchant_balance,
        currency_code=USDC_CODE,
    )
    return merchant_balance, tx_hash


def rebalance_xrp_asset(
    client: JsonRpcClient,
    *,
    merchant_wallet: Wallet,
    buyer_wallet: Wallet,
    merchant_floor_drops: int,
) -> tuple[Decimal, str | None]:
    merchant_balance = get_validated_balance(client, merchant_wallet.classic_address)
    estimated_fee_drops = estimate_xrp_payment_fee_drops(
        client,
        merchant_wallet,
        buyer_wallet.classic_address,
    )
    sendable_drops = (
        merchant_balance
        - merchant_floor_drops
        - estimated_fee_drops
        - XRP_REBALANCE_FEE_CUSHION_DROPS
    )
    if sendable_drops <= 0:
        return Decimal("0"), None

    buyer_starting_balance = get_validated_balance(client, buyer_wallet.classic_address)
    tx_hash = submit_validated_xrp_payment(
        client,
        merchant_wallet,
        buyer_wallet.classic_address,
        sendable_drops,
    )
    wait_for_xrp_balance_increase(
        client,
        buyer_wallet.classic_address,
        starting_balance=buyer_starting_balance,
        increase=sendable_drops,
    )
    merchant_balance_after = get_validated_balance(client, merchant_wallet.classic_address)
    if merchant_balance_after < merchant_floor_drops:
        raise RuntimeError(
            "Merchant XRP balance fell below the configured floor after fees: "
            f"observed {merchant_balance_after} drops, expected at least {merchant_floor_drops} drops."
        )
    return Decimal(sendable_drops), tx_hash


def rebalance_contract_assets(
    contract_path: Path,
    *,
    wallet_cache: Path,
    rebalance_xrp: bool,
    merchant_xrp_floor: Decimal,
) -> list[RebalanceResult]:
    wallet_set = load_cached_demo_wallet_set(wallet_cache)
    if wallet_set is None:
        raise RuntimeError(
            f"Demo wallet cache {wallet_cache} is missing per-asset buyer wallets. "
            "Rerun `python -m devtools.quickstart` first."
        )

    floor_drops = parse_xrp_to_drops(merchant_xrp_floor)
    contract_assets = load_contract_assets(contract_path)
    env_values_by_symbol = {
        asset.symbol: load_env_file(asset.env_path)
        for asset in contract_assets
    }
    rlusd_issuer = (env_values_by_symbol.get("RLUSD", {}).get("PRICE_ASSET_ISSUER") or "").strip() or None
    usdc_issuer = (env_values_by_symbol.get("USDC", {}).get("PRICE_ASSET_ISSUER") or "").strip() or None
    results: list[RebalanceResult] = []

    for asset in contract_assets:
        env_values = env_values_by_symbol[asset.symbol]
        validate_cached_merchant(asset.env_path, env_values, wallet_set.merchant_wallet)
        buyer_wallet = wallet_set.buyer_wallet(asset.symbol.lower())
        validate_cached_buyer(
            asset.env_path,
            env_values,
            asset_symbol=asset.symbol,
            cached_buyer=buyer_wallet,
        )

        rpc_url = (env_values.get("XRPL_RPC_URL") or "").strip()
        if not rpc_url:
            raise RuntimeError(f"{asset.env_path} is missing XRPL_RPC_URL")

        client = JsonRpcClient(rpc_url)

        if asset.symbol == "XRP":
            if rebalance_xrp:
                moved_amount, tx_hash = rebalance_xrp_asset(
                    client,
                    merchant_wallet=wallet_set.merchant_wallet,
                    buyer_wallet=buyer_wallet,
                    merchant_floor_drops=floor_drops,
                )
                status = "rebalanced" if moved_amount > 0 else "noop"
            else:
                moved_amount, tx_hash = Decimal("0"), None
                status = "skipped"
        elif asset.symbol == "RLUSD":
            issuer = (env_values.get("PRICE_ASSET_ISSUER") or "").strip()
            if not issuer:
                raise RuntimeError(f"{asset.env_path} is missing PRICE_ASSET_ISSUER for RLUSD")
            moved_amount, tx_hash = rebalance_rlusd_asset(
                client,
                merchant_wallet=wallet_set.merchant_wallet,
                buyer_wallet=buyer_wallet,
                issuer=issuer,
            )
            status = "rebalanced" if moved_amount > 0 else "noop"
        elif asset.symbol == "USDC":
            issuer = (env_values.get("PRICE_ASSET_ISSUER") or "").strip()
            if not issuer:
                raise RuntimeError(f"{asset.env_path} is missing PRICE_ASSET_ISSUER for USDC")
            moved_amount, tx_hash = rebalance_usdc_asset(
                client,
                merchant_wallet=wallet_set.merchant_wallet,
                buyer_wallet=buyer_wallet,
                issuer=issuer,
            )
            status = "rebalanced" if moved_amount > 0 else "noop"
        else:
            raise RuntimeError(f"Unsupported contract asset {asset.symbol}")

        merchant_balances = capture_wallet_balances(
            client,
            address=wallet_set.merchant_wallet.classic_address,
            rlusd_issuer=rlusd_issuer,
            usdc_issuer=usdc_issuer,
        )
        buyer_balances = capture_wallet_balances(
            client,
            address=buyer_wallet.classic_address,
            rlusd_issuer=rlusd_issuer,
            usdc_issuer=usdc_issuer,
        )
        results.append(
            RebalanceResult(
                symbol=asset.symbol,
                env_path=asset.env_path,
                merchant_address=wallet_set.merchant_wallet.classic_address,
                buyer_address=buyer_wallet.classic_address,
                merchant_balances=merchant_balances,
                buyer_balances=buyer_balances,
                status=status,
                moved_amount=moved_amount,
                tx_hash=tx_hash,
            )
        )

    return results


def print_summary(results: list[RebalanceResult]) -> None:
    for result in results:
        print(f"{result.symbol}: {result.status}")
        print(f"  env: {result.env_path}")
        print(f"  merchant: {result.merchant_address}")
        print(f"  merchant balances: {format_wallet_balances(result.merchant_balances)}")
        print(f"  buyer: {result.buyer_address}")
        print(f"  buyer balances: {format_wallet_balances(result.buyer_balances)}")
        print(f"  moved: {format_amount(result.symbol, result.moved_amount)}")
        if result.tx_hash:
            print(f"  tx hash: {result.tx_hash}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results = rebalance_contract_assets(
        Path(args.contract),
        wallet_cache=Path(args.wallet_cache),
        rebalance_xrp=args.rebalance_xrp,
        merchant_xrp_floor=Decimal(str(args.merchant_xrp_floor)),
    )
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
from typing import Sequence

from xrpl.clients import JsonRpcClient

from devtools.live_testnet_support import (
    TESTNET_RPC_URL,
    default_usdc_issuer,
    get_live_wallet_pair,
    prepare_usdc_topup,
)


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Prepare or recover the cached XRPL Testnet USDC wallet for manual Circle faucet top-ups."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)

    try:
        client = JsonRpcClient(TESTNET_RPC_URL)
        wallets = get_live_wallet_pair(client)
        issuer = default_usdc_issuer()
        result = prepare_usdc_topup(client, wallets, issuer)
    except Exception as exc:
        print(f"USDC top-up failed: {exc}")
        return 1

    print(result.message)
    print(f"Claim state: {result.claim_state_path}")
    print(f"Canonical wallet: {result.canonical_wallet_address}")
    if result.swept_amount > 0:
        print(f"Recovered {result.swept_amount} USDC into the accumulator wallet.")
    if result.deleted_wallets > 0:
        print(f"Deleted {result.deleted_wallets} disposable claim wallet(s).")
    if result.created_claim_wallet_address is not None:
        print(f"Disposable claim wallet: {result.created_claim_wallet_address}")
    if result.cooldown_until is not None:
        print(f"Next local claim window: {result.cooldown_until.isoformat()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

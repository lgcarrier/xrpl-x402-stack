from __future__ import annotations

import argparse
import os
from typing import Sequence

from xrpl.clients import JsonRpcClient

from devtools.live_testnet_support import (
    TRYRLUSD_SESSION_TOKEN_ENV,
    claim_rlusd_topup,
    default_rlusd_issuer,
    get_live_wallet_pair,
    resolve_live_testnet_rpc_url,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Top up the cached XRPL Testnet RLUSD wallet when the local cooldown allows it.",
    )
    parser.add_argument(
        "--xrpl-rpc-url",
        default=None,
        help="Optional XRPL Testnet JSON-RPC endpoint override",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        client = JsonRpcClient(resolve_live_testnet_rpc_url(args.xrpl_rpc_url))
        wallets = get_live_wallet_pair(client)
        issuer = default_rlusd_issuer()
        result = claim_rlusd_topup(
            client,
            wallets,
            issuer,
            session_token=os.environ.get(TRYRLUSD_SESSION_TOKEN_ENV),
        )
    except Exception as exc:
        print(f"RLUSD top-up failed: {exc}")
        return 1

    print(result.message)
    print(f"Claim state: {result.claim_state_path}")
    print(f"Canonical wallet: {result.canonical_wallet_address}")
    if result.swept_amount > 0:
        print(f"Recovered {result.swept_amount} RLUSD into the accumulator wallet.")
    if result.deleted_wallets > 0:
        print(f"Deleted {result.deleted_wallets} disposable claim wallet(s).")

    if result.status == "claimed":
        print(f"Claim tx hash: {result.claim_tx_hash}")
        print(f"Disposable claim wallet: {result.created_claim_wallet_address}")
        print(f"Next local claim window: {result.cooldown_until.isoformat()}")
    elif result.created_claim_wallet_address is not None:
        print(f"Disposable claim wallet: {result.created_claim_wallet_address}")
    elif result.cooldown_until is not None:
        print(f"Next local claim window: {result.cooldown_until.isoformat()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

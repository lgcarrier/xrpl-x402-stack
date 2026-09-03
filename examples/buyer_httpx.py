from __future__ import annotations

import asyncio
import os

import httpx
from xrpl.wallet import Wallet

from xrpl_x402_client import (
    XRPLAssetSpendLimit,
    XRPLPaymentSigner,
    wrap_httpx_with_xrpl_payment,
)
from xrpl_x402_core.testnet_rpc import resolve_testnet_rpc_url

DEFAULT_NETWORK = "xrpl:1"


def build_signer_from_env() -> XRPLPaymentSigner:
    seed = os.getenv("XRPL_WALLET_SEED", "").strip()
    if not seed:
        raise RuntimeError("XRPL_WALLET_SEED is required")
    network = os.getenv("XRPL_NETWORK", DEFAULT_NETWORK)
    rpc_url = os.getenv("XRPL_RPC_URL", "").strip()
    if not rpc_url and network == DEFAULT_NETWORK:
        rpc_url = resolve_testnet_rpc_url()
    return XRPLPaymentSigner(
        Wallet.from_seed(seed),
        rpc_url=rpc_url or "https://s1.ripple.com:51234",
        network=network,
    )


def asset_limits_from_env(network: str) -> list[XRPLAssetSpendLimit] | None:
    asset = os.getenv("PAYMENT_ASSET", "").strip()
    if not asset:
        return None
    cap = os.getenv("PAYMENT_MAX_SPEND", "").strip()
    issuer = os.getenv("PAYMENT_ASSET_ISSUER", "").strip() or None
    if not cap:
        raise RuntimeError("Explicit PAYMENT_ASSET requires PAYMENT_MAX_SPEND")
    return [
        XRPLAssetSpendLimit(
            network=network,
            asset=asset,
            issuer=issuer,
            max_amount=cap,
        )
    ]


async def fetch_paid_resource(
    *,
    signer: XRPLPaymentSigner | None = None,
    target_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    active_signer = signer or build_signer_from_env()
    limits = asset_limits_from_env(active_signer.network)
    async with wrap_httpx_with_xrpl_payment(
        active_signer,
        asset_limits=limits,
        spend_controls=None if limits else {"max_amount_per_payment": "$1"},
        transport=transport,
        timeout=30,
    ) as client:
        return await client.get(
            target_url or os.getenv("TARGET_URL", "http://127.0.0.1:8010/premium")
        )


async def main() -> None:
    response = await fetch_paid_resource()
    print(f"status={response.status_code}")
    print(response.text)


if __name__ == "__main__":
    asyncio.run(main())

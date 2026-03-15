from __future__ import annotations

import asyncio
import os

import httpx
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner, wrap_httpx_with_xrpl_payment


def payment_asset_from_env() -> str | None:
    asset = os.getenv("PAYMENT_ASSET", "").strip()
    return asset or None


def build_signer_from_env() -> XRPLPaymentSigner:
    wallet_seed = os.environ.get("XRPL_WALLET_SEED")
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to run the buyer example")

    wallet = Wallet.from_seed(wallet_seed)
    return XRPLPaymentSigner(
        wallet,
        rpc_url=os.getenv("XRPL_RPC_URL", "https://s.altnet.rippletest.net:51234"),
        network=os.getenv("XRPL_NETWORK", "xrpl:1"),
    )


async def fetch_paid_resource(
    *,
    signer: XRPLPaymentSigner | None = None,
    target_url: str | None = None,
    payment_asset: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    active_signer = signer or build_signer_from_env()
    active_target_url = target_url or os.getenv("TARGET_URL", "http://127.0.0.1:8010/premium")
    active_payment_asset = (
        payment_asset if payment_asset is not None else payment_asset_from_env()
    )
    async with wrap_httpx_with_xrpl_payment(
        active_signer,
        asset=active_payment_asset,
        transport=transport,
    ) as client:
        return await client.get(active_target_url)


async def main() -> None:
    response = await fetch_paid_resource()
    print(f"status={response.status_code}")
    print(response.text)


if __name__ == "__main__":
    asyncio.run(main())

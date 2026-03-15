from __future__ import annotations

import asyncio
import os

from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner, wrap_httpx_with_xrpl_payment


async def main() -> None:
    wallet_seed = os.environ.get("XRPL_WALLET_SEED")
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to run the buyer example")

    wallet = Wallet.from_seed(wallet_seed)
    signer = XRPLPaymentSigner(
        wallet,
        rpc_url=os.getenv("XRPL_RPC_URL", "https://s.altnet.rippletest.net:51234"),
        network=os.getenv("XRPL_NETWORK", "xrpl:1"),
    )
    target_url = os.getenv("TARGET_URL", "http://127.0.0.1:8010/premium")

    async with wrap_httpx_with_xrpl_payment(signer) as client:
        response = await client.get(target_url)

    print(f"status={response.status_code}")
    print(response.text)


if __name__ == "__main__":
    asyncio.run(main())

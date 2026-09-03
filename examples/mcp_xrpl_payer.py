from __future__ import annotations

import asyncio
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

from xrpl_x402_client import XRPLAssetSpendLimit
from xrpl_x402_payer import (
    build_signer_from_env,
    wrap_mcp_client_with_xrpl_payment,
)


async def main() -> None:
    signer = build_signer_from_env()
    server_url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:4022")
    sse_url = server_url.rstrip("/")
    if not sse_url.endswith("/sse"):
        sse_url += "/sse"

    # This must identify the endpoint and authenticated principal across
    # restarts. Use a stable, non-secret subject/tenant label or hash. Never
    # place the bearer token, API key, cookie, or wallet seed in this value.
    principal = os.getenv("MCP_RECOVERY_PRINCIPAL", "local-example-user")
    recovery_scope = f"{server_url}|principal:{principal}"

    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with sse_client(sse_url, headers=headers) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            client = wrap_mcp_client_with_xrpl_payment(
                session,
                signer,
                asset_limits=[
                    XRPLAssetSpendLimit(
                        network=signer.network,
                        asset="XRP",
                        max_amount=os.getenv("PAYMENT_MAX_SPEND", "1000"),
                    )
                ],
                recovery_scope=recovery_scope,
            )
            result = await client.call_tool("generate_report", {"topic": "XRPL"})
            print(result)


if __name__ == "__main__":
    asyncio.run(main())

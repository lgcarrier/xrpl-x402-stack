from __future__ import annotations

import json

from xrpl_x402_payer.payer import budget_status as get_budget_status
from xrpl_x402_payer.payer import format_pay_result, get_receipts, pay_with_x402
from xrpl_x402_payer.proxy import proxy_manager

try:
    from fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - exercised through the CLI help path instead
    FastMCP = None


async def pay_url(
    url: str,
    amount: float = 0.001,
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: float | None = None,
    dry_run: bool = False,
) -> str:
    """Pay for a URL with XRPL x402 and return the response body."""

    result = await pay_with_x402(
        url=url,
        amount=amount,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
    )
    return format_pay_result(result)


async def list_receipts(limit: int = 10) -> str:
    """List recent x402 payment receipts."""

    receipts = get_receipts(limit=limit)
    if not receipts:
        return "No receipts recorded yet."
    return "\n".join(
        f"- {receipt['url']} -> {receipt['amount']} {receipt['asset_identifier'].split(':', 1)[0]} ({receipt['tx_hash']})"
        for receipt in receipts
    )


async def budget_status(asset: str = "XRP", issuer: str | None = None) -> str:
    """Show local spend totals and remaining budget for an asset."""

    summary = get_budget_status(asset=asset, issuer=issuer)
    return json.dumps(summary, indent=2, sort_keys=True)


async def proxy_mode(
    target_base_url: str,
    local_port: int = 8787,
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: float | None = None,
    dry_run: bool = False,
) -> str:
    """Start or reuse the local x402 payer forward proxy."""

    bind_url = proxy_manager.start(
        target_base_url=target_base_url,
        port=local_port,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
    )
    return f"Proxy ready at {bind_url} -> {target_base_url}"


if FastMCP is not None:
    mcp = FastMCP(
        name="xrpl-x402-payer",
        instructions=(
            "Pay for 402-protected XRPL x402 resources. "
            "Use pay_url for one-off requests, list_receipts for audit history, "
            "budget_status for local spend tracking, and proxy_mode to launch a local proxy."
        ),
    )
    mcp.tool(pay_url)
    mcp.tool(list_receipts)
    mcp.tool(budget_status)
    mcp.tool(proxy_mode)
else:
    mcp = None


def main() -> None:
    if mcp is None:
        raise RuntimeError(
            "FastMCP is not installed. Reinstall with: pip install \"xrpl-x402-payer[mcp]\""
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

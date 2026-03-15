from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
import shutil

import typer

from xrpl_x402_payer.payer import budget_status as get_budget_status
from xrpl_x402_payer.payer import format_pay_result, get_receipts, pay_with_x402
from xrpl_x402_payer.proxy import run_proxy

app = typer.Typer(help="XRPL x402 payer CLI")
skill_app = typer.Typer(help="Bundled skill helpers")
app.add_typer(skill_app, name="skill")


@app.command()
def pay(
    url: str,
    amount: float = typer.Option(0.001, help="Default spend cap in asset units"),
    asset: str = typer.Option("XRP", help="Asset code to pay with"),
    issuer: str | None = typer.Option(None, help="Issuer for issued assets"),
    max_spend: float | None = typer.Option(None, help="Explicit spend cap override"),
    dry_run: bool = typer.Option(False, help="Preview the request without signing or retrying"),
) -> None:
    """Pay for a URL using XRPL x402."""

    result = asyncio.run(
        pay_with_x402(
            url=url,
            amount=amount,
            asset=asset,
            issuer=issuer,
            max_spend=max_spend,
            dry_run=dry_run,
        )
    )
    typer.echo(format_pay_result(result))


@app.command()
def proxy(
    target_base_url: str,
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8787, help="Bind port"),
    amount: float = typer.Option(0.001, help="Default spend cap in asset units"),
    asset: str = typer.Option("XRP", help="Asset code to pay with"),
    issuer: str | None = typer.Option(None, help="Issuer for issued assets"),
    max_spend: float | None = typer.Option(None, help="Explicit spend cap override"),
    dry_run: bool = typer.Option(False, help="Preview proxy requests without paying"),
) -> None:
    """Run the local x402 auto-pay forward proxy."""

    run_proxy(
        target_base_url=target_base_url,
        host=host,
        port=port,
        amount=amount,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
    )


@skill_app.command("install")
def skill_install(destination: Path | None = typer.Option(None, help="Optional target skill directory")) -> None:
    """Install the bundled payer skill into ~/.agents/skills."""

    source_path = Path(__file__).resolve().parent / "skills" / "xrpl-x402-payer" / "SKILL.md"
    target_dir = (
        destination.expanduser()
        if destination is not None
        else Path.home() / ".agents" / "skills" / "xrpl-x402-payer"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_dir / "SKILL.md")
    typer.echo(f"Installed skill to {target_dir / 'SKILL.md'}")


@app.command()
def receipts(limit: int = typer.Option(10, help="Maximum receipts to show")) -> None:
    """Show recent local payment receipts."""

    receipts = get_receipts(limit=limit)
    if not receipts:
        typer.echo("No receipts recorded yet.")
        return
    for receipt in receipts:
        typer.echo(
            f"{receipt['url']} -> {receipt['amount']} {receipt['asset_identifier']} ({receipt['tx_hash']})"
        )


@app.command()
def budget(
    asset: str = typer.Option("XRP", help="Asset code to summarize"),
    issuer: str | None = typer.Option(None, help="Issuer for issued assets"),
) -> None:
    """Show current local spend totals for an asset."""

    typer.echo(json.dumps(get_budget_status(asset=asset, issuer=issuer), indent=2, sort_keys=True))


@app.command()
def mcp(stdio: bool = True) -> None:
    """Run the official XRPL x402 MCP server for local agents."""

    if stdio:
        importlib.import_module("xrpl_x402_payer.mcp").main()
        return

    raise typer.BadParameter("Only stdio transport is supported in this release.")


def main() -> None:
    app()

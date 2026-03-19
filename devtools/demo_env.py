from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from devtools.live_testnet_support import default_rlusd_issuer, default_usdc_issuer
from devtools.quickstart import DEFAULT_PRICE_DROPS
from xrpl_x402_core import NETWORK_RLUSD_ISSUERS, NETWORK_USDC_ISSUERS, asset_identifier_from_parts

DEFAULT_BASE_PATH = Path(".env.quickstart")
DEFAULT_RLUSD_AMOUNT = "1.25"
DEFAULT_USDC_AMOUNT = "2.50"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a derived quickstart env file for the XRP, RLUSD, or USDC Docker demo."
        ),
    )
    parser.add_argument(
        "--asset",
        choices=("xrp", "rlusd", "usdc"),
        required=True,
        help="Demo asset to configure in the derived env file",
    )
    parser.add_argument(
        "--base",
        default=str(DEFAULT_BASE_PATH),
        help="Base quickstart env file to copy from",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path for the generated env file; defaults to <base>.<asset>",
    )
    parser.add_argument(
        "--issuer",
        default=None,
        help="Optional issued-asset issuer override for RLUSD or USDC",
    )
    parser.add_argument(
        "--amount",
        default=None,
        help="Optional issued-asset amount override for RLUSD or USDC",
    )
    parser.add_argument(
        "--price-drops",
        type=int,
        default=None,
        help="Optional XRP drop amount override for the XRP demo",
    )
    return parser


def parse_env_lines(text: str) -> list[str]:
    return text.splitlines()


def get_env_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def set_env_value(lines: list[str], key: str, value: str) -> None:
    rendered = f"{key}={value}"
    prefix = f"{key}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = rendered
            return
    lines.append(rendered)


def derived_output_path(base_path: Path, asset: str) -> Path:
    return Path(f"{base_path}.{asset}")


def built_in_issuer(asset: str, network_id: str) -> str | None:
    if asset == "rlusd":
        return NETWORK_RLUSD_ISSUERS.get(network_id)
    if asset == "usdc":
        return NETWORK_USDC_ISSUERS.get(network_id)
    return None


def demo_allowed_issued_assets(*, asset: str, issuer: str | None, network_id: str) -> str:
    if issuer is None:
        return ""
    if issuer == built_in_issuer(asset, network_id):
        return ""
    code = asset.upper()
    return asset_identifier_from_parts(code, issuer)


def configure_demo_env(
    *,
    lines: list[str],
    asset: str,
    network_id: str,
    issuer: str | None,
    amount: str | None,
    price_drops: int | None,
) -> None:
    if asset == "xrp":
        resolved_price_drops = price_drops or int(
            get_env_value(lines, "PRICE_DROPS") or str(DEFAULT_PRICE_DROPS)
        )
        set_env_value(lines, "PRICE_DROPS", str(resolved_price_drops))
        set_env_value(lines, "PRICE_ASSET_CODE", "XRP")
        set_env_value(lines, "PRICE_ASSET_ISSUER", "")
        set_env_value(lines, "PRICE_ASSET_AMOUNT", "")
        set_env_value(lines, "PAYMENT_ASSET", "XRP:native")
        set_env_value(lines, "ALLOWED_ISSUED_ASSETS", "")
        return

    if issuer is None or amount is None:
        raise ValueError(f"{asset} demos require both an issuer and an amount")

    code = asset.upper()
    set_env_value(lines, "PRICE_ASSET_CODE", code)
    set_env_value(lines, "PRICE_ASSET_ISSUER", issuer)
    set_env_value(lines, "PRICE_ASSET_AMOUNT", amount)
    set_env_value(lines, "PAYMENT_ASSET", asset_identifier_from_parts(code, issuer))
    set_env_value(
        lines,
        "ALLOWED_ISSUED_ASSETS",
        demo_allowed_issued_assets(asset=asset, issuer=issuer, network_id=network_id),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    base_path = Path(args.base)
    if not base_path.exists():
        raise FileNotFoundError(
            f"{base_path} does not exist. Run `python -m devtools.quickstart` first."
        )

    output_path = Path(args.output) if args.output else derived_output_path(base_path, args.asset)
    lines = parse_env_lines(base_path.read_text(encoding="utf-8"))
    network_id = (
        get_env_value(lines, "NETWORK_ID")
        or get_env_value(lines, "XRPL_NETWORK")
        or "xrpl:1"
    ).strip() or "xrpl:1"

    issuer: str | None = None
    amount: str | None = None
    if args.asset == "rlusd":
        issuer = args.issuer or default_rlusd_issuer()
        amount = args.amount or DEFAULT_RLUSD_AMOUNT
    elif args.asset == "usdc":
        issuer = args.issuer or default_usdc_issuer()
        amount = args.amount or DEFAULT_USDC_AMOUNT

    configure_demo_env(
        lines=lines,
        asset=args.asset,
        network_id=network_id,
        issuer=issuer,
        amount=amount,
        price_drops=args.price_drops,
    )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Wrote {output_path}")
    print(f"Network: {network_id}")
    print(f"Demo asset: {args.asset.upper()}")
    print(f"Buyer payment asset: {get_env_value(lines, 'PAYMENT_ASSET')}")
    if args.asset == "xrp":
        print(f"Merchant price drops: {get_env_value(lines, 'PRICE_DROPS')}")
    else:
        print(
            "Merchant price: "
            f"{get_env_value(lines, 'PRICE_ASSET_AMOUNT')} "
            f"{get_env_value(lines, 'PRICE_ASSET_CODE')}"
        )
        print(f"Merchant issuer: {get_env_value(lines, 'PRICE_ASSET_ISSUER')}")
    allowed_issued_assets = get_env_value(lines, "ALLOWED_ISSUED_ASSETS") or ""
    if allowed_issued_assets:
        print(f"Facilitator extra issued assets: {allowed_issued_assets}")
    print("Next steps:")
    print(f"  docker compose --env-file {output_path} up --build")
    print(f"  docker compose --env-file {output_path} --profile demo run --rm buyer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

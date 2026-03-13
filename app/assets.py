from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import string

XRP_CODE = "XRP"
RLUSD_CODE = "RLUSD"
RLUSD_HEX = "524C555344000000000000000000000000000000"
RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_TESTNET_ISSUER = "rnEVYfAWYP5HpPaWQiPSJMyDeUiEJ6zhy2"
TF_PARTIAL_PAYMENT = 0x00020000

NETWORK_RLUSD_ISSUERS = {
    "xrpl:0": RLUSD_MAINNET_ISSUER,
    "xrpl:1": RLUSD_TESTNET_ISSUER,
}

_XRP_DROPS_PER_XRP = Decimal("1000000")
_HEX_DIGITS = frozenset(string.hexdigits)


@dataclass(frozen=True)
class AssetKey:
    code: str
    issuer: str | None = None


@dataclass(frozen=True)
class NormalizedAmount:
    asset: AssetKey
    value: Decimal
    drops: int | None = None


def normalize_currency_code(currency: str) -> str:
    normalized = str(currency).strip().upper()
    if not normalized:
        raise ValueError("Issued asset currency missing")

    if len(normalized) == 40 and set(normalized) <= _HEX_DIGITS:
        decoded = bytes.fromhex(normalized)
        decoded_ascii = decoded.rstrip(b"\x00")
        if decoded_ascii and all(32 <= byte <= 126 for byte in decoded_ascii):
            normalized = decoded_ascii.decode("ascii").upper()

    return normalized


def parse_allowed_issued_assets(raw_assets: str) -> list[AssetKey]:
    parsed_assets: list[AssetKey] = []
    seen_assets: set[AssetKey] = set()

    for raw_entry in raw_assets.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue

        code, separator, issuer = entry.partition(":")
        normalized_code = normalize_currency_code(code)
        normalized_issuer = issuer.strip()
        if not separator or not normalized_issuer:
            raise ValueError(
                "ALLOWED_ISSUED_ASSETS entries must use CODE:ISSUER format"
            )
        if normalized_code == XRP_CODE:
            raise ValueError("ALLOWED_ISSUED_ASSETS cannot include XRP")

        asset = AssetKey(code=normalized_code, issuer=normalized_issuer)
        if asset not in seen_assets:
            parsed_assets.append(asset)
            seen_assets.add(asset)

    return parsed_assets


def supported_asset_keys(network_id: str, raw_assets: str) -> list[AssetKey]:
    supported_assets = [AssetKey(code=XRP_CODE, issuer=None)]
    seen_assets = {supported_assets[0]}

    built_in_rlusd_issuer = NETWORK_RLUSD_ISSUERS.get(network_id)
    if built_in_rlusd_issuer:
        built_in_rlusd = AssetKey(code=RLUSD_CODE, issuer=built_in_rlusd_issuer)
        supported_assets.append(built_in_rlusd)
        seen_assets.add(built_in_rlusd)

    for asset in parse_allowed_issued_assets(raw_assets):
        if asset not in seen_assets:
            supported_assets.append(asset)
            seen_assets.add(asset)

    return supported_assets


def format_decimal(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def format_amount(amount: NormalizedAmount) -> str:
    if amount.drops is not None:
        xrp_value = Decimal(amount.drops) / _XRP_DROPS_PER_XRP
        return f"{format_decimal(xrp_value)} {XRP_CODE}"
    return f"{format_decimal(amount.value)} {amount.asset.code}"

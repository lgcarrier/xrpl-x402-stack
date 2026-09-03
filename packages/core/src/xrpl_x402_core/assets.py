from __future__ import annotations

from dataclasses import dataclass
import string

XRP_CODE = "XRP"
RLUSD_CODE = "RLUSD"
RLUSD_HEX = "524C555344000000000000000000000000000000"
RLUSD_MAINNET_ISSUER = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"
LEGACY_RLUSD_TESTNET_ISSUER = "rnEVYfAWYP5HpPaWQiPSJMyDeUiEJ6zhy2"
USDC_CODE = "USDC"
USDC_HEX = "5553444300000000000000000000000000000000"
USDC_MAINNET_ISSUER = "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"
USDC_TESTNET_ISSUER = "rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt"
TF_PARTIAL_PAYMENT = 0x00020000

NETWORK_RLUSD_ISSUERS = {
    "xrpl:0": RLUSD_MAINNET_ISSUER,
    "xrpl:1": RLUSD_TESTNET_ISSUER,
}
NETWORK_USDC_ISSUERS = {
    "xrpl:0": USDC_MAINNET_ISSUER,
    "xrpl:1": USDC_TESTNET_ISSUER,
}

_HEX_DIGITS = frozenset(string.hexdigits)

DEFAULT_ASSETS = {
    "xrpl:0": [
        {
            "asset": RLUSD_HEX,
            "decimals": 15,
            "symbol": RLUSD_CODE,
            "issuer": RLUSD_MAINNET_ISSUER,
        }
    ],
    "xrpl:1": [
        {
            "asset": RLUSD_HEX,
            "decimals": 15,
            "symbol": RLUSD_CODE,
            "issuer": RLUSD_TESTNET_ISSUER,
        }
    ],
}


def find_default_asset(asset: str, network: str) -> dict[str, object] | None:
    for entry in DEFAULT_ASSETS.get(str(network), []):
        if str(entry["asset"]).upper() == str(asset).upper():
            return dict(entry)
    return None


def get_default_asset(network: str, symbol: str | None = None) -> dict[str, object]:
    assets = DEFAULT_ASSETS.get(str(network), [])
    if not assets:
        raise ValueError(f"No default asset configured for network {network}")
    if symbol is None:
        return dict(assets[0])
    for entry in assets:
        if str(entry["symbol"]).upper() == symbol.upper():
            return dict(entry)
    raise ValueError(f"No {symbol} default asset configured for network {network}")


@dataclass(frozen=True)
class AssetKey:
    code: str
    issuer: str | None = None


def normalize_currency_code(currency: str) -> str:
    normalized = str(currency).strip().upper()
    if not normalized:
        raise ValueError("Asset code is required")
    if len(normalized) == 40 and set(normalized) <= _HEX_DIGITS:
        decoded = bytes.fromhex(normalized).rstrip(b"\x00")
        if decoded and all(32 <= byte <= 126 for byte in decoded):
            normalized = decoded.decode("ascii").upper()
    return normalized


def xrpl_currency_code(code: str) -> str:
    raw_code = str(code).strip()
    if not raw_code:
        raise ValueError("Asset code is required")
    if len(raw_code) == 40 and set(raw_code) <= _HEX_DIGITS:
        return raw_code.upper()
    normalized = normalize_currency_code(raw_code)
    if len(normalized) == 3:
        return normalized
    if len(normalized) > 20:
        raise ValueError("Issued currency codes must be 20 ASCII bytes or fewer")
    return normalized.encode("ascii").hex().upper().ljust(40, "0")


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
        if (
            normalized_code == RLUSD_CODE
            and normalized_issuer == LEGACY_RLUSD_TESTNET_ISSUER
        ):
            raise ValueError(
                "The former XRPL Testnet RLUSD issuer is not canonical; update the issuer explicitly"
            )
        asset = AssetKey(code=normalized_code, issuer=normalized_issuer)
        if asset not in seen_assets:
            parsed_assets.append(asset)
            seen_assets.add(asset)
    return parsed_assets


def supported_asset_keys(network_id: str, raw_assets: str) -> list[AssetKey]:
    supported_assets = [AssetKey(code=XRP_CODE, issuer=None)]
    seen_assets = {supported_assets[0]}
    for code, issuer in ((RLUSD_CODE, NETWORK_RLUSD_ISSUERS.get(network_id)),):
        if not issuer:
            continue
        asset = AssetKey(code=code, issuer=issuer)
        if asset not in seen_assets:
            supported_assets.append(asset)
            seen_assets.add(asset)
    for asset in parse_allowed_issued_assets(raw_assets):
        if asset not in seen_assets:
            supported_assets.append(asset)
            seen_assets.add(asset)
    return supported_assets

from xrpl_x402_core import (
    RLUSD_MAINNET_ISSUER,
    RLUSD_HEX,
    RLUSD_TESTNET_ISSUER,
    USDC_HEX,
    USDC_MAINNET_ISSUER,
    USDC_TESTNET_ISSUER,
    supported_asset_keys,
    xrpl_currency_code,
)


def test_supported_asset_keys_include_builtin_mainnet_issued_assets() -> None:
    assets = supported_asset_keys("xrpl:0", "")

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_MAINNET_ISSUER),
        ("USDC", USDC_MAINNET_ISSUER),
    ]


def test_supported_asset_keys_include_builtin_testnet_issued_assets() -> None:
    assets = supported_asset_keys("xrpl:1", "")

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_TESTNET_ISSUER),
        ("USDC", USDC_TESTNET_ISSUER),
    ]


def test_supported_asset_keys_deduplicate_builtin_and_extra_assets() -> None:
    assets = supported_asset_keys(
        "xrpl:1",
        f"USDC:{USDC_TESTNET_ISSUER},EUR:rExtraIssuer,RLUSD:{RLUSD_TESTNET_ISSUER}",
    )

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_TESTNET_ISSUER),
        ("USDC", USDC_TESTNET_ISSUER),
        ("EUR", "rExtraIssuer"),
    ]


def test_xrpl_currency_code_keeps_standard_codes_and_hex_encodes_longer_codes() -> None:
    assert xrpl_currency_code("USD") == "USD"
    assert xrpl_currency_code("RLUSD") == RLUSD_HEX
    assert xrpl_currency_code("USDC") == USDC_HEX
    assert xrpl_currency_code(RLUSD_HEX.lower()) == RLUSD_HEX

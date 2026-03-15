from xrpl_x402_core import (
    RLUSD_MAINNET_ISSUER,
    RLUSD_TESTNET_ISSUER,
    USDC_MAINNET_ISSUER,
    USDC_TESTNET_ISSUER,
    supported_asset_keys,
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

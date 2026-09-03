import pytest

from xrpl_x402_core import (
    LEGACY_RLUSD_TESTNET_ISSUER,
    RLUSD_MAINNET_ISSUER,
    RLUSD_HEX,
    RLUSD_TESTNET_ISSUER,
    USDC_HEX,
    USDC_MAINNET_ISSUER,
    USDC_TESTNET_ISSUER,
    supported_asset_keys,
    xrpl_currency_code,
)


def test_supported_asset_keys_default_to_xrp_and_recognized_mainnet_rlusd() -> None:
    assets = supported_asset_keys("xrpl:0", "")

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_MAINNET_ISSUER),
    ]


def test_supported_asset_keys_default_to_xrp_and_recognized_testnet_rlusd() -> None:
    assets = supported_asset_keys("xrpl:1", "")

    assert [(asset.code, asset.issuer) for asset in assets] == [
        ("XRP", None),
        ("RLUSD", RLUSD_TESTNET_ISSUER),
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


def test_usdc_requires_explicit_allowlist_entry() -> None:
    assert ("USDC", USDC_MAINNET_ISSUER) not in {
        (asset.code, asset.issuer) for asset in supported_asset_keys("xrpl:0", "")
    }
    assert ("USDC", USDC_MAINNET_ISSUER) in {
        (asset.code, asset.issuer)
        for asset in supported_asset_keys(
            "xrpl:0", f"USDC:{USDC_MAINNET_ISSUER}"
        )
    }


def test_former_testnet_rlusd_issuer_is_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="former XRPL Testnet RLUSD issuer"):
        supported_asset_keys(
            "xrpl:1", f"RLUSD:{LEGACY_RLUSD_TESTNET_ISSUER}"
        )


def test_xrpl_currency_code_keeps_standard_codes_and_hex_encodes_longer_codes() -> None:
    assert xrpl_currency_code("USD") == "USD"
    assert xrpl_currency_code("RLUSD") == RLUSD_HEX
    assert xrpl_currency_code("USDC") == USDC_HEX
    assert xrpl_currency_code(RLUSD_HEX.lower()) == RLUSD_HEX

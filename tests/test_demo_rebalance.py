from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from xrpl.wallet import Wallet

import devtools.demo_rebalance as demo_rebalance
from devtools.live_testnet_support import DemoWalletSet


def write_env(path: Path, *, merchant: str, buyer_seed: str, asset_code: str, issuer: str = "") -> None:
    path.write_text(
        "\n".join(
            [
                "XRPL_RPC_URL=https://resolved.testnet.rpc/",
                "NETWORK_ID=xrpl:1",
                "XRPL_NETWORK=xrpl:1",
                f"MY_DESTINATION_ADDRESS={merchant}",
                "FACILITATOR_BEARER_TOKEN=test-token",
                f"XRPL_WALLET_SEED={buyer_seed}",
                "PRICE_DROPS=1000",
                f"PRICE_ASSET_CODE={asset_code}",
                f"PRICE_ASSET_ISSUER={issuer}",
                "PRICE_ASSET_AMOUNT=1.25",
                (
                    f"PAYMENT_ASSET={asset_code}:{issuer}"
                    if issuer
                    else "PAYMENT_ASSET=XRP:native"
                ),
                "ALLOWED_ISSUED_ASSETS=",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_contract(path: Path, *, xrp_env: Path, rlusd_env: Path, usdc_env: Path) -> None:
    payload = {
        "execution": {
            "env_files": {
                "XRP": xrp_env.name,
                "RLUSD": rlusd_env.name,
                "USDC": usdc_env.name,
            }
        },
        "assets": [
            {"symbol": "XRP"},
            {"symbol": "RLUSD"},
            {"symbol": "USDC"},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def build_demo_wallets() -> DemoWalletSet:
    return DemoWalletSet(
        merchant_wallet=Wallet.create(),
        buyers={
            "xrp": Wallet.create(),
            "rlusd": Wallet.create(),
            "usdc": Wallet.create(),
        },
    )


def test_rebalance_rlusd_asset_transfers_full_merchant_balance_to_buyer(monkeypatch) -> None:
    merchant_wallet = Wallet.create()
    buyer_wallet = Wallet.create()
    balances = {
        merchant_wallet.classic_address: Decimal("4.5"),
        buyer_wallet.classic_address: Decimal("1.25"),
    }
    tx_hash = "ABC123"

    monkeypatch.setattr(demo_rebalance, "ensure_rlusd_trustline", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        demo_rebalance,
        "get_validated_trustline_balance",
        lambda _client, address, _issuer, *, currency_code="RLUSD": balances[address],
    )

    def fake_submit(_client, wallet, destination_address, _issuer, amount):
        assert wallet.classic_address == merchant_wallet.classic_address
        assert destination_address == buyer_wallet.classic_address
        balances[merchant_wallet.classic_address] -= amount
        balances[buyer_wallet.classic_address] += amount
        return tx_hash

    monkeypatch.setattr(demo_rebalance, "submit_validated_rlusd_payment", fake_submit)
    monkeypatch.setattr(
        demo_rebalance,
        "wait_for_trustline_balance_increase",
        lambda *_args, **_kwargs: balances[buyer_wallet.classic_address],
    )

    moved_amount, observed_tx_hash = demo_rebalance.rebalance_rlusd_asset(
        object(),
        merchant_wallet=merchant_wallet,
        buyer_wallet=buyer_wallet,
        issuer="rRLUSDIssuer",
    )

    assert moved_amount == Decimal("4.5")
    assert observed_tx_hash == tx_hash
    assert balances[merchant_wallet.classic_address] == Decimal("0")
    assert balances[buyer_wallet.classic_address] == Decimal("5.75")


def test_rebalance_xrp_asset_respects_floor(monkeypatch) -> None:
    merchant_wallet = Wallet.create()
    buyer_wallet = Wallet.create()
    balances = {
        merchant_wallet.classic_address: 100_500_000,
        buyer_wallet.classic_address: 99_000_000,
    }
    tx_hash = "XRPHASH"

    monkeypatch.setattr(
        demo_rebalance,
        "get_validated_balance",
        lambda _client, address: balances[address],
    )
    monkeypatch.setattr(
        demo_rebalance,
        "estimate_xrp_payment_fee_drops",
        lambda _client, _wallet, _destination_address: 12,
    )

    def fake_submit(_client, wallet, destination_address, amount_drops):
        assert wallet.classic_address == merchant_wallet.classic_address
        assert destination_address == buyer_wallet.classic_address
        assert amount_drops == 499_938
        balances[merchant_wallet.classic_address] -= amount_drops + 12
        balances[buyer_wallet.classic_address] += amount_drops
        return tx_hash

    monkeypatch.setattr(demo_rebalance, "submit_validated_xrp_payment", fake_submit)
    monkeypatch.setattr(
        demo_rebalance,
        "wait_for_xrp_balance_increase",
        lambda *_args, **_kwargs: balances[buyer_wallet.classic_address],
    )

    moved_amount, observed_tx_hash = demo_rebalance.rebalance_xrp_asset(
        object(),
        merchant_wallet=merchant_wallet,
        buyer_wallet=buyer_wallet,
        merchant_floor_drops=100_000_000,
    )

    assert moved_amount == Decimal("499938")
    assert observed_tx_hash == tx_hash
    assert balances[merchant_wallet.classic_address] == 100_000_050
    assert balances[buyer_wallet.classic_address] == 99_499_938


def test_rebalance_xrp_asset_returns_noop_when_fee_reserve_consumes_spendable_balance(monkeypatch) -> None:
    merchant_wallet = Wallet.create()
    buyer_wallet = Wallet.create()
    balances = {
        merchant_wallet.classic_address: 100_000_061,
        buyer_wallet.classic_address: 99_000_000,
    }
    submit_called = {"value": False}

    monkeypatch.setattr(
        demo_rebalance,
        "get_validated_balance",
        lambda _client, address: balances[address],
    )
    monkeypatch.setattr(
        demo_rebalance,
        "estimate_xrp_payment_fee_drops",
        lambda _client, _wallet, _destination_address: 12,
    )
    monkeypatch.setattr(
        demo_rebalance,
        "submit_validated_xrp_payment",
        lambda *_args, **_kwargs: submit_called.__setitem__("value", True),
    )

    moved_amount, observed_tx_hash = demo_rebalance.rebalance_xrp_asset(
        object(),
        merchant_wallet=merchant_wallet,
        buyer_wallet=buyer_wallet,
        merchant_floor_drops=100_000_000,
    )

    assert moved_amount == Decimal("0")
    assert observed_tx_hash is None
    assert submit_called["value"] is False


def test_rebalance_xrp_asset_rejects_post_fee_balance_below_floor(monkeypatch) -> None:
    merchant_wallet = Wallet.create()
    buyer_wallet = Wallet.create()
    balances = {
        merchant_wallet.classic_address: 100_500_000,
        buyer_wallet.classic_address: 99_000_000,
    }

    monkeypatch.setattr(
        demo_rebalance,
        "get_validated_balance",
        lambda _client, address: balances[address],
    )
    monkeypatch.setattr(
        demo_rebalance,
        "estimate_xrp_payment_fee_drops",
        lambda _client, _wallet, _destination_address: 12,
    )

    def fake_submit(_client, wallet, destination_address, amount_drops):
        assert wallet.classic_address == merchant_wallet.classic_address
        assert destination_address == buyer_wallet.classic_address
        assert amount_drops == 499_938
        balances[merchant_wallet.classic_address] -= amount_drops + 100
        balances[buyer_wallet.classic_address] += amount_drops
        return "XRPHASH"

    monkeypatch.setattr(demo_rebalance, "submit_validated_xrp_payment", fake_submit)
    monkeypatch.setattr(
        demo_rebalance,
        "wait_for_xrp_balance_increase",
        lambda *_args, **_kwargs: balances[buyer_wallet.classic_address],
    )

    with pytest.raises(RuntimeError, match="Merchant XRP balance fell below the configured floor after fees"):
        demo_rebalance.rebalance_xrp_asset(
            object(),
            merchant_wallet=merchant_wallet,
            buyer_wallet=buyer_wallet,
            merchant_floor_drops=100_000_000,
        )


def test_rebalance_contract_assets_dispatches_from_contract_envs(monkeypatch, tmp_path: Path) -> None:
    wallets = build_demo_wallets()
    contract_path = tmp_path / "demo.contract.json"
    xrp_env = tmp_path / ".env.quickstart"
    rlusd_env = tmp_path / ".env.quickstart.rlusd"
    usdc_env = tmp_path / ".env.quickstart.usdc"

    write_env(
        xrp_env,
        merchant=wallets.merchant_wallet.classic_address,
        buyer_seed=(wallets.buyer_wallet("xrp").seed or ""),
        asset_code="XRP",
    )
    write_env(
        rlusd_env,
        merchant=wallets.merchant_wallet.classic_address,
        buyer_seed=(wallets.buyer_wallet("rlusd").seed or ""),
        asset_code="RLUSD",
        issuer="rRLUSDIssuer",
    )
    write_env(
        usdc_env,
        merchant=wallets.merchant_wallet.classic_address,
        buyer_seed=(wallets.buyer_wallet("usdc").seed or ""),
        asset_code="USDC",
        issuer="rUSDCTestIssuer",
    )
    write_contract(contract_path, xrp_env=xrp_env, rlusd_env=rlusd_env, usdc_env=usdc_env)

    monkeypatch.setattr(demo_rebalance, "load_cached_demo_wallet_set", lambda _path: wallets)
    monkeypatch.setattr(demo_rebalance, "JsonRpcClient", lambda url: {"rpc_url": url})

    calls: list[tuple[str, str]] = []

    def fake_rebalance_xrp(_client, *, merchant_wallet, buyer_wallet, merchant_floor_drops):
        calls.append(("XRP", buyer_wallet.classic_address))
        assert merchant_wallet.classic_address == wallets.merchant_wallet.classic_address
        assert merchant_floor_drops == 100_000_000
        return Decimal("2500"), "xrp-tx"

    def fake_rebalance_rlusd(_client, *, merchant_wallet, buyer_wallet, issuer):
        calls.append(("RLUSD", buyer_wallet.classic_address))
        assert merchant_wallet.classic_address == wallets.merchant_wallet.classic_address
        assert issuer == "rRLUSDIssuer"
        return Decimal("3.75"), "rlusd-tx"

    def fake_rebalance_usdc(_client, *, merchant_wallet, buyer_wallet, issuer):
        calls.append(("USDC", buyer_wallet.classic_address))
        assert merchant_wallet.classic_address == wallets.merchant_wallet.classic_address
        assert issuer == "rUSDCTestIssuer"
        return Decimal("4.5"), "usdc-tx"

    monkeypatch.setattr(demo_rebalance, "rebalance_xrp_asset", fake_rebalance_xrp)
    monkeypatch.setattr(demo_rebalance, "rebalance_rlusd_asset", fake_rebalance_rlusd)
    monkeypatch.setattr(demo_rebalance, "rebalance_usdc_asset", fake_rebalance_usdc)
    monkeypatch.setattr(
        demo_rebalance,
        "capture_wallet_balances",
        lambda _client, *, address, rlusd_issuer, usdc_issuer: demo_rebalance.WalletBalances(
            xrp_drops=100_000_000 if address == wallets.merchant_wallet.classic_address else 99_000_000,
            rlusd_balance=Decimal("1.25") if address == wallets.merchant_wallet.classic_address else Decimal("30"),
            usdc_balance=Decimal("2.5") if address == wallets.merchant_wallet.classic_address else Decimal("20"),
        ),
    )

    results = demo_rebalance.rebalance_contract_assets(
        contract_path,
        wallet_cache=tmp_path / "wallet-cache.json",
        rebalance_xrp=True,
        merchant_xrp_floor=Decimal("100"),
    )

    assert [result.symbol for result in results] == ["XRP", "RLUSD", "USDC"]
    assert calls == [
        ("XRP", wallets.buyer_wallet("xrp").classic_address),
        ("RLUSD", wallets.buyer_wallet("rlusd").classic_address),
        ("USDC", wallets.buyer_wallet("usdc").classic_address),
    ]
    assert [result.status for result in results] == ["rebalanced", "rebalanced", "rebalanced"]
    assert all(result.merchant_address == wallets.merchant_wallet.classic_address for result in results)
    assert results[0].merchant_balances == demo_rebalance.WalletBalances(
        xrp_drops=100_000_000,
        rlusd_balance=Decimal("1.25"),
        usdc_balance=Decimal("2.5"),
    )
    assert results[1].buyer_balances == demo_rebalance.WalletBalances(
        xrp_drops=99_000_000,
        rlusd_balance=Decimal("30"),
        usdc_balance=Decimal("20"),
    )


def test_rebalance_contract_assets_rejects_merchant_mismatch(monkeypatch, tmp_path: Path) -> None:
    wallets = build_demo_wallets()
    contract_path = tmp_path / "demo.contract.json"
    xrp_env = tmp_path / ".env.quickstart"
    rlusd_env = tmp_path / ".env.quickstart.rlusd"
    usdc_env = tmp_path / ".env.quickstart.usdc"

    wrong_merchant = Wallet.create().classic_address
    write_env(
        xrp_env,
        merchant=wrong_merchant,
        buyer_seed=(wallets.buyer_wallet("xrp").seed or ""),
        asset_code="XRP",
    )
    write_env(
        rlusd_env,
        merchant=wrong_merchant,
        buyer_seed=(wallets.buyer_wallet("rlusd").seed or ""),
        asset_code="RLUSD",
        issuer="rRLUSDIssuer",
    )
    write_env(
        usdc_env,
        merchant=wrong_merchant,
        buyer_seed=(wallets.buyer_wallet("usdc").seed or ""),
        asset_code="USDC",
        issuer="rUSDCTestIssuer",
    )
    write_contract(contract_path, xrp_env=xrp_env, rlusd_env=rlusd_env, usdc_env=usdc_env)

    monkeypatch.setattr(demo_rebalance, "load_cached_demo_wallet_set", lambda _path: wallets)

    with pytest.raises(RuntimeError, match="cached merchant wallet"):
        demo_rebalance.rebalance_contract_assets(
            contract_path,
            wallet_cache=tmp_path / "wallet-cache.json",
            rebalance_xrp=False,
            merchant_xrp_floor=Decimal("100"),
        )


@pytest.mark.parametrize("asset_symbol", ["XRP", "RLUSD"])
def test_rebalance_contract_assets_rejects_buyer_mismatch(
    monkeypatch,
    tmp_path: Path,
    asset_symbol: str,
) -> None:
    wallets = build_demo_wallets()
    contract_path = tmp_path / "demo.contract.json"
    wrong_buyer = Wallet.create()
    asset_key = asset_symbol.lower()
    env_path = tmp_path / (
        ".env.quickstart"
        if asset_symbol == "XRP"
        else f".env.quickstart.{asset_key}"
    )

    write_env(
        env_path,
        merchant=wallets.merchant_wallet.classic_address,
        buyer_seed=wrong_buyer.seed or "",
        asset_code=asset_symbol,
        issuer="rRLUSDIssuer" if asset_symbol == "RLUSD" else "",
    )
    contract_path.write_text(
        json.dumps(
            {
                "execution": {"env_files": {asset_symbol: env_path.name}},
                "assets": [{"symbol": asset_symbol}],
            }
        ),
        encoding="utf-8",
    )

    client_created = {"value": False}
    monkeypatch.setattr(demo_rebalance, "load_cached_demo_wallet_set", lambda _path: wallets)
    monkeypatch.setattr(
        demo_rebalance,
        "JsonRpcClient",
        lambda _url: client_created.__setitem__("value", True),
    )

    with pytest.raises(
        RuntimeError,
        match="Regenerate the env file from the current quickstart wallet cache before rebalancing",
    ) as exc_info:
        demo_rebalance.rebalance_contract_assets(
            contract_path,
            wallet_cache=tmp_path / "wallet-cache.json",
            rebalance_xrp=True,
            merchant_xrp_floor=Decimal("100"),
        )

    cached_buyer = wallets.buyer_wallet(asset_key)
    assert str(env_path.resolve()) in str(exc_info.value)
    assert f"{asset_symbol} buyer {wrong_buyer.classic_address}" in str(exc_info.value)
    assert cached_buyer.classic_address in str(exc_info.value)
    assert client_created["value"] is False


def test_print_summary_includes_wallet_balance_lines(capsys) -> None:
    demo_rebalance.print_summary(
        [
            demo_rebalance.RebalanceResult(
                symbol="RLUSD",
                env_path=Path("/tmp/.env.quickstart.rlusd"),
                merchant_address="rMerchant",
                buyer_address="rBuyer",
                merchant_balances=demo_rebalance.WalletBalances(
                    xrp_drops=100_001_610,
                    rlusd_balance=Decimal("0"),
                    usdc_balance=Decimal("2.5"),
                ),
                buyer_balances=demo_rebalance.WalletBalances(
                    xrp_drops=99_999_960,
                    rlusd_balance=Decimal("30"),
                    usdc_balance=Decimal("0"),
                ),
                status="noop",
                moved_amount=Decimal("0"),
                tx_hash=None,
            )
        ]
    )

    captured = capsys.readouterr()
    assert "merchant: rMerchant" in captured.out
    assert (
        "merchant balances: XRP 100.001610 XRP, RLUSD 0 RLUSD, USDC 2.5 USDC"
        in captured.out
    )
    assert "buyer: rBuyer" in captured.out
    assert (
        "buyer balances: XRP 99.999960 XRP, RLUSD 30 RLUSD, USDC 0 USDC"
        in captured.out
    )

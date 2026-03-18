from __future__ import annotations

from pathlib import Path

from xrpl.wallet import Wallet

import devtools.quickstart as quickstart
from devtools.live_testnet_support import LiveWalletPair


def test_render_quickstart_env_contains_expected_values() -> None:
    merchant_wallet = Wallet.create()
    buyer_wallet = Wallet.create()

    rendered = quickstart.render_quickstart_env(
        xrpl_rpc_url="https://resolved.testnet.rpc/",
        merchant_wallet=merchant_wallet,
        buyer_wallet=buyer_wallet,
        facilitator_token="quickstart-token",
        price_drops=2500,
    )

    assert f"MY_DESTINATION_ADDRESS={merchant_wallet.classic_address}" in rendered
    assert f"XRPL_WALLET_SEED={buyer_wallet.seed}" in rendered
    assert "FACILITATOR_BEARER_TOKEN=quickstart-token" in rendered
    assert "XRPL_RPC_URL=https://resolved.testnet.rpc/" in rendered
    assert "PRICE_DROPS=2500" in rendered
    assert "PAYMENT_ASSET=XRP:native" in rendered


def test_quickstart_main_writes_env_file_and_prints_commands(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    merchant_wallet = Wallet.create()
    buyer_wallet = Wallet.create()
    output_path = tmp_path / ".env.quickstart"
    call_state = {"rpc_url": None}

    monkeypatch.setattr(
        quickstart,
        "resolve_live_testnet_rpc_url",
        lambda explicit_rpc_url=None: "https://resolved.testnet.rpc/",
    )
    monkeypatch.setattr(
        quickstart,
        "JsonRpcClient",
        lambda url: call_state.__setitem__("rpc_url", url) or ("client", url),
    )
    monkeypatch.setattr(
        quickstart,
        "get_live_wallet_pair",
        lambda _client: LiveWalletPair(wallet_a=merchant_wallet, wallet_b=buyer_wallet),
    )
    monkeypatch.setattr(quickstart.secrets, "token_urlsafe", lambda _size: "generated-token")
    monkeypatch.setattr(quickstart, "wallet_cache_path", lambda: tmp_path / "wallet-cache.json")

    exit_code = quickstart.main(["--output", str(output_path), "--price-drops", "1500"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert call_state["rpc_url"] == "https://resolved.testnet.rpc/"
    assert output_path.read_text(encoding="utf-8")
    assert "Wrote" in captured.out
    assert "docker compose --env-file" in captured.out
    assert str(output_path) in captured.out
    assert f"Buyer address: {buyer_wallet.classic_address}" in captured.out
    assert "XRPL RPC URL: https://resolved.testnet.rpc/" in captured.out
    assert "XRPL_RPC_URL=https://resolved.testnet.rpc/" in output_path.read_text(encoding="utf-8")
    assert "generated-token" in output_path.read_text(encoding="utf-8")

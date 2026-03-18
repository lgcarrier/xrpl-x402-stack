from __future__ import annotations

import pytest

import xrpl_x402_core.testnet_rpc as testnet_rpc


def test_resolve_testnet_rpc_url_returns_explicit_override_without_probe(
    monkeypatch,
) -> None:
    probe_called = {"value": False}

    def fake_probe(*_args, **_kwargs) -> int:
        probe_called["value"] = True
        return testnet_rpc.TESTNET_NETWORK_ID

    monkeypatch.setattr(testnet_rpc, "probe_rpc_network_id", fake_probe)

    assert (
        testnet_rpc.resolve_testnet_rpc_url(explicit_url=" https://custom.testnet.rpc/ ")
        == "https://custom.testnet.rpc/"
    )
    assert probe_called["value"] is False


def test_resolve_testnet_rpc_url_skips_unhealthy_candidates(monkeypatch) -> None:
    attempted_urls: list[str] = []

    def fake_probe(rpc_url: str, *, timeout_seconds: float) -> int:
        attempted_urls.append(rpc_url)
        if rpc_url == "https://first.testnet.rpc/":
            raise RuntimeError("connection reset")
        if rpc_url == "https://second.testnet.rpc/":
            return 0
        return testnet_rpc.TESTNET_NETWORK_ID

    monkeypatch.setattr(testnet_rpc, "probe_rpc_network_id", fake_probe)

    resolved_url = testnet_rpc.resolve_testnet_rpc_url(
        candidate_urls=(
            "https://first.testnet.rpc/",
            "https://second.testnet.rpc/",
            "https://third.testnet.rpc/",
        )
    )

    assert resolved_url == "https://third.testnet.rpc/"
    assert attempted_urls == [
        "https://first.testnet.rpc/",
        "https://second.testnet.rpc/",
        "https://third.testnet.rpc/",
    ]


def test_resolve_testnet_rpc_url_rejects_non_testnet_network_id(monkeypatch) -> None:
    monkeypatch.setattr(testnet_rpc, "probe_rpc_network_id", lambda *_args, **_kwargs: 0)

    with pytest.raises(
        testnet_rpc.TestnetRPCResolutionError,
        match="reported network_id 0",
    ):
        testnet_rpc.resolve_testnet_rpc_url(candidate_urls=("https://mainnet.rpc/",))


def test_resolve_testnet_rpc_url_lists_attempted_urls_on_failure(monkeypatch) -> None:
    def fake_probe(rpc_url: str, *, timeout_seconds: float) -> int:
        if rpc_url == "https://first.testnet.rpc/":
            raise RuntimeError("timeout")
        raise RuntimeError("connection reset")

    monkeypatch.setattr(testnet_rpc, "probe_rpc_network_id", fake_probe)

    with pytest.raises(testnet_rpc.TestnetRPCResolutionError) as exc_info:
        testnet_rpc.resolve_testnet_rpc_url(
            candidate_urls=(
                "https://first.testnet.rpc/",
                "https://second.testnet.rpc/",
            )
        )

    message = str(exc_info.value)
    assert "https://first.testnet.rpc/" in message
    assert "https://second.testnet.rpc/" in message
    assert "timeout" in message
    assert "connection reset" in message

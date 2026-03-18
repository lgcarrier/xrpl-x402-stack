from __future__ import annotations

import json
from collections.abc import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PUBLIC_TESTNET_RPC_URLS: tuple[str, ...] = (
    "https://testnet.xrpl-labs.com/",
    "https://s.altnet.rippletest.net:51234/",
)
TESTNET_NETWORK_ID = 1
DEFAULT_RPC_TIMEOUT_SECONDS = 5.0
DEFAULT_USER_AGENT = "xrpl-x402-stack/0.1"


class TestnetRPCResolutionError(RuntimeError):
    """Raised when no healthy XRPL Testnet JSON-RPC endpoint can be found."""


def resolve_testnet_rpc_url(
    *,
    explicit_url: str | None = None,
    candidate_urls: Sequence[str] = PUBLIC_TESTNET_RPC_URLS,
    timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
) -> str:
    resolved_explicit_url = (explicit_url or "").strip()
    if resolved_explicit_url:
        return resolved_explicit_url

    attempts: list[str] = []
    for candidate_url in candidate_urls:
        resolved_candidate_url = candidate_url.strip()
        if not resolved_candidate_url:
            continue

        try:
            network_id = probe_rpc_network_id(
                resolved_candidate_url,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            attempts.append(f"{resolved_candidate_url} ({exc})")
            continue

        if network_id != TESTNET_NETWORK_ID:
            attempts.append(
                f"{resolved_candidate_url} (reported network_id {network_id})"
            )
            continue

        return resolved_candidate_url

    attempted_urls = ", ".join(attempts) if attempts else "none"
    raise TestnetRPCResolutionError(
        "Unable to resolve a healthy XRPL Testnet JSON-RPC endpoint. "
        f"Attempted: {attempted_urls}"
    )


def probe_rpc_network_id(
    rpc_url: str,
    *,
    timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
) -> int:
    request = Request(
        rpc_url,
        data=json.dumps({"method": "server_info", "params": [{}]}).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "user-agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        message = f"HTTP {exc.code}"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc

    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("missing result object in server_info response")

    if result.get("status") != "success":
        raise ValueError(f"unexpected server_info status {result.get('status')!r}")

    info = result.get("info")
    if not isinstance(info, dict):
        raise ValueError("missing result.info in server_info response")

    network_id = info.get("network_id")
    if network_id is None:
        raise ValueError("missing result.info.network_id in server_info response")

    try:
        return int(str(network_id))
    except ValueError as exc:
        raise ValueError(f"invalid network_id value {network_id!r}") from exc


__all__ = [
    "DEFAULT_RPC_TIMEOUT_SECONDS",
    "DEFAULT_USER_AGENT",
    "PUBLIC_TESTNET_RPC_URLS",
    "TESTNET_NETWORK_ID",
    "TestnetRPCResolutionError",
    "probe_rpc_network_id",
    "resolve_testnet_rpc_url",
]

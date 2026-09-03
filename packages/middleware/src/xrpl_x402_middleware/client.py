from __future__ import annotations

from typing import Any

from x402.http import (
    CreateHeadersAuthProvider,
    FacilitatorConfig,
    HTTPFacilitatorClient,
)


class XRPLFacilitatorClient(HTTPFacilitatorClient):
    """Official HTTP facilitator client specialized for XRPL registration."""


def build_facilitator_client(
    *,
    base_url: str,
    bearer_token: str,
    timeout: float = 30.0,
    http_client: Any = None,
) -> XRPLFacilitatorClient:
    token = bearer_token.strip()
    if not token:
        raise ValueError("bearer_token is required")
    auth = CreateHeadersAuthProvider(
        lambda: {
            "verify": {"Authorization": f"Bearer {token}"},
            "settle": {"Authorization": f"Bearer {token}"},
        }
    )
    return XRPLFacilitatorClient(
        FacilitatorConfig(
            url=base_url,
            timeout=timeout,
            http_client=http_client,
            auth_provider=auth,
        )
    )


__all__ = ["XRPLFacilitatorClient", "build_facilitator_client"]

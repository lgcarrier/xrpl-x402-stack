from __future__ import annotations

import asyncio
from typing import Any

from x402.http import (
    CreateHeadersAuthProvider,
    FacilitatorConfig,
    HTTPFacilitatorClient,
)
from x402.schemas import PaymentPayload, PaymentRequirements, SettleResponse


class XRPLFacilitatorClient(HTTPFacilitatorClient):
    """Official HTTP facilitator client with bounded pending reconciliation."""

    def __init__(
        self,
        config: FacilitatorConfig | dict[str, Any] | None = None,
        *,
        pending_attempts: int = 3,
        pending_backoff_seconds: tuple[float, ...] = (0.1, 0.25, 0.5),
    ) -> None:
        super().__init__(config)
        if pending_attempts < 0:
            raise ValueError("pending_attempts cannot be negative")
        self._pending_attempts = pending_attempts
        self._pending_backoff_seconds = pending_backoff_seconds

    async def settle(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> SettleResponse:
        result = await super().settle(payload, requirements)
        for attempt in range(self._pending_attempts):
            if not _is_pending(result):
                break
            delay = self._pending_backoff_seconds[
                min(attempt, len(self._pending_backoff_seconds) - 1)
            ] if self._pending_backoff_seconds else 0
            if delay:
                await asyncio.sleep(delay)
            # The same immutable envelope is retried. The facilitator reconciles
            # the reserved transaction hash and must never rebroadcast it.
            result = await super().settle(payload, requirements)
        return result


def build_facilitator_client(
    *,
    base_url: str,
    bearer_token: str,
    timeout: float = 30.0,
    http_client: Any = None,
    pending_attempts: int = 3,
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
        ),
        pending_attempts=pending_attempts,
    )


def _is_pending(result: SettleResponse) -> bool:
    return (
        not result.success
        and result.error_reason == "settlement_pending"
        and bool(result.transaction)
        and bool(result.network)
    )


__all__ = ["XRPLFacilitatorClient", "build_facilitator_client"]

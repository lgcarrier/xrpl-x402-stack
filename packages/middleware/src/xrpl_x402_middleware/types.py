from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from x402.http import RouteConfig
from x402.schemas import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResponse,
)


@dataclass(frozen=True, slots=True)
class AcceptedRequirementsContext:
    scheme: str
    network: str
    asset: str
    amount: str
    pay_to: str
    max_timeout_seconds: int
    extra: Mapping[str, Any]

    @classmethod
    def from_requirements(
        cls, requirements: PaymentRequirements
    ) -> "AcceptedRequirementsContext":
        return cls(
            scheme=requirements.scheme,
            network=str(requirements.network),
            asset=requirements.asset,
            amount=requirements.amount,
            pay_to=requirements.pay_to,
            max_timeout_seconds=requirements.max_timeout_seconds,
            extra=MappingProxyType(dict(requirements.extra or {})),
        )


@dataclass(frozen=True, slots=True)
class XRPLPaymentContext:
    settlement: SettleResponse
    accepted: AcceptedRequirementsContext


__all__ = [
    "AcceptedRequirementsContext",
    "PaymentPayload",
    "PaymentRequired",
    "PaymentRequirements",
    "RouteConfig",
    "SettleResponse",
    "XRPLPaymentContext",
]

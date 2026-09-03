from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from x402.schemas import PaymentRequirements

from xrpl_x402_core import normalize_currency_code, xrpl_currency_code


@dataclass(frozen=True, slots=True)
class XRPLAssetSpendLimit:
    """Explicit opt-in and per-payment cap for a non-default XRPL asset."""

    network: str
    asset: str
    max_amount: str
    issuer: str | None = None

    def __post_init__(self) -> None:
        try:
            cap = Decimal(self.max_amount)
        except InvalidOperation as exc:
            raise ValueError("max_amount must be a decimal string") from exc
        if not cap.is_finite() or cap <= 0:
            raise ValueError("max_amount must be positive and finite")
        if normalize_currency_code(self.asset) != "XRP" and not self.issuer:
            raise ValueError(
                "Non-XRP spend limits require an explicit issuer"
            )


def apply_xrpl_spend_limits(
    client: Any, limits: list[XRPLAssetSpendLimit]
) -> Any:
    """Configure upstream allowlisting plus issuer-aware decimal caps."""

    if not limits:
        return client
    client.set_spend_controls(
        {
            "max_amount_per_payment": False,
            "allowed_assets": [
                {
                    "network": limit.network,
                    "asset": xrpl_currency_code(limit.asset),
                    "max_amount_per_payment": (
                        limit.max_amount
                        if normalize_currency_code(limit.asset) == "XRP"
                        else None
                    ),
                }
                for limit in limits
            ],
        }
    )

    def policy(
        _version: int, requirements: list[PaymentRequirements]
    ) -> list[PaymentRequirements]:
        accepted: list[PaymentRequirements] = []
        for requirement in requirements:
            code = normalize_currency_code(requirement.asset)
            issuer = (requirement.extra or {}).get("issuer")
            for limit in limits:
                if (
                    str(requirement.network) == limit.network
                    and code == normalize_currency_code(limit.asset)
                    and issuer == limit.issuer
                    and Decimal(requirement.amount) <= Decimal(limit.max_amount)
                ):
                    accepted.append(requirement)
                    break
        return accepted

    client.register_policy(policy)
    return client

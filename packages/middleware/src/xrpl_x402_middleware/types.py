from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator, model_validator

from xrpl_x402_core import (
    PaymentPayload,
    PaymentRequired,
    PaymentResponse,
    XRPLAmount,
    XRPLAsset,
    XRPLPaymentOption,
    XRPLPaymentPayload,
)
from xrpl_x402_core.models import StrictModel


class RouteConfig(StrictModel):
    facilitator_url: str = Field(alias="facilitatorUrl")
    bearer_token: str = Field(alias="bearerToken", repr=False)
    accepts: list[XRPLPaymentOption]
    description: str | None = None
    mime_type: str = Field(default="application/json", alias="mimeType")

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("facilitator_url", "bearer_token")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @model_validator(mode="after")
    def _validate_accepts(self) -> "RouteConfig":
        if not self.accepts:
            raise ValueError("At least one payment option is required")
        return self


__all__ = [
    "PaymentPayload",
    "PaymentRequired",
    "PaymentResponse",
    "RouteConfig",
    "XRPLAmount",
    "XRPLAsset",
    "XRPLPaymentOption",
    "XRPLPaymentPayload",
]

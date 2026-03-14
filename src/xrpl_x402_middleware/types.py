from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xrpl_x402_middleware.utils import (
    build_xrpl_extra,
    is_valid_xrpl_network,
    normalize_currency_code,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


class XRPLAsset(StrictModel):
    code: str
    issuer: str | None = None

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        return normalize_currency_code(value)

    @field_validator("issuer")
    @classmethod
    def _normalize_issuer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class XRPLAmount(StrictModel):
    value: str
    unit: Literal["drops", "issued"]
    drops: int | None = None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Amount value is required")
        return normalized

    @model_validator(mode="after")
    def _validate_amount(self) -> "XRPLAmount":
        if self.unit == "drops":
            if self.drops is None:
                try:
                    self.drops = int(self.value)
                except ValueError as exc:
                    raise ValueError("Drops amount must be an integer") from exc
            if self.drops < 0:
                raise ValueError("Drops amount must be zero or greater")
            if str(self.drops) != self.value:
                raise ValueError("Drops amount must match the integer value string")
            return self

        if self.drops is not None:
            raise ValueError("Issued-asset amounts cannot set drops")
        return self


class XRPLPaymentOption(WireModel):
    scheme: Literal["exact"] = "exact"
    network: str
    pay_to: str = Field(alias="payTo")
    max_amount_required: str = Field(alias="maxAmountRequired")
    asset: XRPLAsset
    amount: XRPLAmount
    description: str | None = None
    mime_type: str = Field(default="application/json", alias="mimeType")
    expires_at: int | None = Field(default=None, alias="expiresAt")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_xrpl_network(normalized):
            raise ValueError("network must be a CAIP-2 xrpl:<reference> identifier")
        return normalized

    @model_validator(mode="after")
    def _sync_fields(self) -> "XRPLPaymentOption":
        if self.max_amount_required != self.amount.value:
            raise ValueError("maxAmountRequired must match amount.value")

        extra = dict(self.extra)
        extra.setdefault("xrpl", build_xrpl_extra(self.asset, self.amount)["xrpl"])
        self.extra = extra
        return self


class RouteConfig(StrictModel):
    facilitator_url: str = Field(alias="facilitatorUrl")
    bearer_token: str = Field(alias="bearerToken", repr=False)
    accepts: list[XRPLPaymentOption]
    description: str | None = None
    mime_type: str = Field(default="application/json", alias="mimeType")

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


class XRPLPaymentPayload(WireModel):
    signed_tx_blob: str = Field(alias="signedTxBlob")
    invoice_id: str | None = Field(default=None, alias="invoiceId")


class PaymentPayload(WireModel):
    x402_version: Literal[2] = Field(default=2, alias="x402Version")
    scheme: Literal["exact"] = "exact"
    network: str
    payload: XRPLPaymentPayload

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_xrpl_network(normalized):
            raise ValueError("network must be a CAIP-2 xrpl:<reference> identifier")
        return normalized


class PaymentRequired(WireModel):
    x402_version: Literal[2] = Field(default=2, alias="x402Version")
    error: str
    accepts: list[XRPLPaymentOption]


class PaymentResponse(WireModel):
    x402_version: Literal[2] = Field(default=2, alias="x402Version")
    scheme: Literal["exact"] = "exact"
    network: str
    success: bool = True
    payer: str
    pay_to: str = Field(alias="payTo")
    invoice_id: str = Field(alias="invoiceId")
    tx_hash: str = Field(alias="txHash")
    settlement_status: Literal["submitted", "validated"] = Field(alias="settlementStatus")
    asset: XRPLAsset
    amount: XRPLAmount

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_xrpl_network(normalized):
            raise ValueError("network must be a CAIP-2 xrpl:<reference> identifier")
        return normalized

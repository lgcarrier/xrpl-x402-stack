from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xrpl_x402_core.assets import normalize_currency_code
from xrpl_x402_core.helpers import build_xrpl_extra, is_valid_xrpl_network

SIGNED_TX_BLOB_MAX_LENGTH = 16_384
INVOICE_ID_MAX_LENGTH = 128


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


class PaymentRequest(StrictModel):
    signed_tx_blob: str | None = Field(default=None, max_length=SIGNED_TX_BLOB_MAX_LENGTH)
    invoice_id: str | None = Field(default=None, max_length=INVOICE_ID_MAX_LENGTH)


class StructuredAmount(StrictModel):
    value: str
    unit: Literal["drops", "issued"]
    asset: XRPLAsset
    drops: int | None = None


class FacilitatorVerifyResponse(StrictModel):
    valid: bool
    invoice_id: str
    amount: str
    asset: XRPLAsset
    amount_details: StructuredAmount
    payer: str
    destination: str
    message: str = "Payment valid"


class FacilitatorSettleResponse(StrictModel):
    settled: bool
    tx_hash: str
    status: Literal["submitted", "validated"]


class FacilitatorSupportedResponse(StrictModel):
    network: str
    assets: list[XRPLAsset]
    settlement_mode: Literal["optimistic", "validated"]

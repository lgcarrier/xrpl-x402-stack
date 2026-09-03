from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from x402.schemas import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleRequest,
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyRequest,
    VerifyResponse,
)

SIGNED_TX_BLOB_MAX_LENGTH = 16_384
INVOICE_ID_MAX_LENGTH = 128


class StrictModel(BaseModel):
    """Base model for XRPL mechanism data owned by this project."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        str_strip_whitespace=True,
    )


class ExactXRPLPayload(StrictModel):
    """Inner payload for the x402 exact XRPL mechanism."""

    # This model is parsed directly from the protocol wire. Accept only the
    # canonical v2 alias; the Python field name is the legacy wire spelling.
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
        serialize_by_alias=True,
        str_strip_whitespace=True,
    )

    signed_tx_blob: str = Field(alias="signedTxBlob", max_length=SIGNED_TX_BLOB_MAX_LENGTH)

    @field_validator("signed_tx_blob")
    @classmethod
    def _validate_blob(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized or len(normalized) % 2 or any(
            char not in "0123456789ABCDEF" for char in normalized
        ):
            raise ValueError(
                "signedTxBlob must be an even-length hexadecimal XRPL transaction blob"
            )
        return normalized


class ExactXRPLExtra(StrictModel):
    """Typed view of XRPL fields carried in PaymentRequirements.extra."""

    issuer: str | None = None
    are_fees_sponsored: bool = Field(default=False, alias="areFeesSponsored")
    asset_transfer_method: str | None = Field(
        default=None, alias="assetTransferMethod"
    )
    payment_flow: Literal["authorization"] | None = Field(
        default=None, alias="paymentFlow"
    )
    invoice_id: str | None = Field(
        default=None, alias="invoiceId", max_length=INVOICE_ID_MAX_LENGTH
    )
    destination_tag: int | None = Field(
        default=None, alias="destinationTag", ge=0, le=0xFFFFFFFF
    )


class XRPLSettlementState(StrictModel):
    """Durable state associated with a signed XRPL transaction hash."""

    transaction: str
    network: str
    payer: str | None = None
    first_ledger_sequence: int
    last_ledger_sequence: int
    payment_fingerprint: str
    status: Literal["pending", "failed", "validated"]
    result: dict[str, Any] | None = None


__all__ = [
    "ExactXRPLExtra",
    "ExactXRPLPayload",
    "INVOICE_ID_MAX_LENGTH",
    "PaymentPayload",
    "PaymentRequired",
    "PaymentRequirements",
    "ResourceInfo",
    "SIGNED_TX_BLOB_MAX_LENGTH",
    "SettleRequest",
    "SettleResponse",
    "StrictModel",
    "SupportedKind",
    "SupportedResponse",
    "VerifyRequest",
    "VerifyResponse",
    "XRPLSettlementState",
]

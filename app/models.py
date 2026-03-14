from typing import Literal

from pydantic import BaseModel, Field

SIGNED_TX_BLOB_MAX_LENGTH = 16_384
INVOICE_ID_MAX_LENGTH = 128


class AssetDescriptor(BaseModel):
    code: str
    issuer: str | None = None


class PaymentRequest(BaseModel):
    signed_tx_blob: str | None = Field(default=None, max_length=SIGNED_TX_BLOB_MAX_LENGTH)
    invoice_id: str | None = Field(default=None, max_length=INVOICE_ID_MAX_LENGTH)


class StructuredAmount(BaseModel):
    value: str
    unit: Literal["drops", "issued"]
    asset: AssetDescriptor
    drops: int | None = None


class VerifyResponse(BaseModel):
    valid: bool
    invoice_id: str
    amount: str
    asset: AssetDescriptor
    amount_details: StructuredAmount
    payer: str
    destination: str
    message: str = "Payment valid"


class SettleResponse(BaseModel):
    settled: bool
    tx_hash: str
    status: Literal["submitted", "validated"]


class SupportedResponse(BaseModel):
    network: str
    assets: list[AssetDescriptor]
    settlement_mode: Literal["optimistic", "validated"]

from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict
from x402.schemas import PaymentRequirements, SettleResponse

from xrpl_x402_core import normalize_currency_code
from xrpl_x402_payer.journal import ReceiptJournal

DEFAULT_RECEIPTS_PATH = Path.home() / ".xrpl-x402" / "receipts.jsonl"
RECEIPTS_PATH_ENV = "XRPL_X402_RECEIPTS_PATH"


class ReceiptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: str
    url: str
    method: str
    status_code: int
    state: Literal["paid", "pending", "failed"]
    settlement: SettleResponse
    accepted: PaymentRequirements
    request_fingerprint: str | None = None

    @property
    def amount_decimal(self) -> Decimal:
        return Decimal(self.accepted.amount)


def receipt_store_path() -> Path:
    raw_path = os.getenv(RECEIPTS_PATH_ENV, "").strip()
    return Path(raw_path).expanduser() if raw_path else DEFAULT_RECEIPTS_PATH


class ReceiptStore(ReceiptJournal):
    def __init__(self, path: Path | None = None) -> None:
        super().__init__(path or receipt_store_path())

    def budget_summary(
        self,
        *,
        network: str,
        asset: str,
        issuer: str | None,
        max_spend: Decimal | None = None,
    ) -> dict[str, str | None]:
        spent = sum(
            record.amount_decimal
            for record in self.all()
            if record.state == "paid"
            and str(record.accepted.network) == network
            and normalize_currency_code(record.accepted.asset)
            == normalize_currency_code(asset)
            and (record.accepted.extra or {}).get("issuer") == issuer
        )
        remaining = max_spend - spent if max_spend is not None else None
        return {
            "network": network,
            "asset": asset,
            "issuer": issuer,
            "spent": _format_decimal(spent),
            "max_spend": (
                _format_decimal(max_spend) if max_spend is not None else None
            ),
            "remaining": (
                _format_decimal(remaining) if remaining is not None else None
            ),
        }


def _format_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict


DEFAULT_RECEIPTS_PATH = Path.home() / ".xrpl-x402" / "receipts.jsonl"
RECEIPTS_PATH_ENV = "XRPL_X402_RECEIPTS_PATH"


class ReceiptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: str
    url: str
    method: str
    status_code: int
    network: str
    asset_identifier: str
    amount: str
    payer: str
    tx_hash: str
    settlement_status: str

    @property
    def amount_decimal(self) -> Decimal:
        return Decimal(self.amount)


def receipt_store_path() -> Path:
    raw_path = os.getenv(RECEIPTS_PATH_ENV, "").strip()
    if not raw_path:
        return DEFAULT_RECEIPTS_PATH
    return Path(raw_path).expanduser()


class ReceiptStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or receipt_store_path()

    def append(self, receipt: ReceiptRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(receipt.model_dump_json())
            handle.write("\n")

    def list(self, limit: int = 10) -> list[ReceiptRecord]:
        if limit <= 0 or not self.path.exists():
            return []

        receipts: list[ReceiptRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                receipts.append(ReceiptRecord.model_validate_json(line))
        return list(reversed(receipts[-limit:]))

    def budget_summary(
        self,
        *,
        asset_identifier: str,
        max_spend: Decimal | None = None,
    ) -> dict[str, str | None]:
        spent = sum(
            receipt.amount_decimal
            for receipt in self.list(limit=10_000)
            if receipt.asset_identifier == asset_identifier
        )
        remaining = max_spend - spent if max_spend is not None else None
        return {
            "asset_identifier": asset_identifier,
            "spent": _format_decimal(spent),
            "max_spend": _format_decimal(max_spend) if max_spend is not None else None,
            "remaining": _format_decimal(remaining) if remaining is not None else None,
        }


def _format_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"

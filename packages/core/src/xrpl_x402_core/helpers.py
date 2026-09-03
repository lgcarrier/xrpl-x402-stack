from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from xrpl_x402_core.assets import (
    find_default_asset,
    normalize_currency_code,
    xrpl_currency_code,
)
from xrpl_x402_core.models import (
    ExactXRPLExtra,
    ExactXRPLPayload,
    PaymentPayload,
    PaymentRequirements,
    ResourceInfo,
)

CAIP_2_NETWORK_PATTERN = re.compile(r"^xrpl:(0|[1-9][0-9]*)$")
INTEGER_AMOUNT_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
DECIMAL_AMOUNT_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
XRPL_LEDGER_CLOSE_SECONDS = 5


def is_valid_xrpl_network(network: str) -> bool:
    return bool(CAIP_2_NETWORK_PATTERN.fullmatch(str(network).strip()))


def parse_xrpl_network_id(network: str) -> int:
    normalized = str(network).strip()
    if not is_valid_xrpl_network(normalized):
        raise ValueError("network must be a numeric CAIP-2 xrpl:<reference> identifier")
    return int(normalized.partition(":")[2])


def get_max_last_ledger_sequence(
    current_ledger: int, requirements: PaymentRequirements
) -> int:
    return (
        current_ledger
        + math.ceil(requirements.max_timeout_seconds / XRPL_LEDGER_CLOSE_SECONDS)
        + 2
    )


def invoice_id_to_invoice_id_field(invoice_id: str) -> str:
    normalized = str(invoice_id)
    if not normalized:
        raise ValueError("extra.invoiceId must be non-empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()


def signed_transaction_hash(signed_tx_blob: str) -> str:
    """Return the canonical XRPL transaction id for a signed blob."""

    from xrpl.core import binarycodec

    binarycodec.decode(signed_tx_blob)
    return hashlib.sha512(
        bytes.fromhex("54584E00" + signed_tx_blob)
    ).hexdigest()[:64].upper()


def exact_xrpl_payload(payload: PaymentPayload) -> ExactXRPLPayload:
    return ExactXRPLPayload.model_validate(payload.payload)


def exact_xrpl_extra(requirements: PaymentRequirements) -> ExactXRPLExtra:
    return ExactXRPLExtra.model_validate(requirements.extra or {})


def compare_decimal_strings(left: str, right: str) -> int:
    try:
        left_value = Decimal(left)
        right_value = Decimal(right)
    except InvalidOperation as exc:
        raise ValueError("XRPL IOU amount must be a decimal string") from exc
    return (left_value > right_value) - (left_value < right_value)


def validate_requirements_shape(
    requirements: PaymentRequirements,
) -> ExactXRPLExtra:
    if requirements.scheme != "exact":
        raise ValueError(f"Unsupported scheme: {requirements.scheme}")
    if not is_valid_xrpl_network(str(requirements.network)):
        raise ValueError(f"Unsupported XRPL network: {requirements.network}")
    if requirements.max_timeout_seconds <= 0:
        raise ValueError("maxTimeoutSeconds must be greater than zero")
    try:
        canonical_asset = xrpl_currency_code(requirements.asset)
    except ValueError as exc:
        raise ValueError("Invalid XRPL asset currency code") from exc
    if requirements.asset != canonical_asset:
        raise ValueError(
            "XRPL wire assets must use XRP, an uppercase three-character "
            "currency code, or an uppercase 40-character hexadecimal code"
        )
    if (
        requirements.asset != "XRP"
        and normalize_currency_code(requirements.asset) == "XRP"
    ):
        raise ValueError("XRP cannot be represented as an issued currency")
    raw_extra = requirements.extra or {}
    extra = exact_xrpl_extra(requirements)
    if raw_extra.get("areFeesSponsored") is not False:
        raise ValueError("XRPL exact payments require extra.areFeesSponsored to be false")
    method = extra.asset_transfer_method or "sequence"
    if method not in {"sequence", "ticketSequence"}:
        raise ValueError(f"Unsupported assetTransferMethod: {method}")
    if requirements.asset == "XRP":
        if not INTEGER_AMOUNT_PATTERN.fullmatch(requirements.amount):
            raise ValueError("XRPL native payments require an integer drops amount")
        if int(requirements.amount) <= 0:
            raise ValueError("XRPL exact payment amount must be greater than zero")
        if extra.issuer is not None:
            raise ValueError("XRPL native payments must not include extra.issuer")
    else:
        if not extra.issuer:
            raise ValueError("XRPL IOU payments require extra.issuer")
        default = find_default_asset(
            requirements.asset, str(requirements.network)
        )
        if default is not None and extra.issuer != default.get("issuer"):
            raise ValueError(
                f"XRPL {default['symbol']} payments require extra.issuer "
                f"to be {default['issuer']}"
            )
        if not DECIMAL_AMOUNT_PATTERN.fullmatch(requirements.amount):
            raise ValueError("XRPL IOU payments require a decimal ledger amount")
        if Decimal(requirements.amount) <= 0:
            raise ValueError("XRPL exact payment amount must be greater than zero")
    return extra


def requirements_fingerprint(
    payload: PaymentPayload,
    requirements: PaymentRequirements,
    *,
    resource_method: str | None = None,
    authoritative_resource: ResourceInfo | None = None,
    resource_identity: str | None = None,
) -> str:
    resource_info = authoritative_resource or payload.resource
    resource = (
        resource_info.model_dump(by_alias=True, exclude_none=True)
        if resource_info
        else None
    )
    normalized: dict[str, Any] = {
        "accepted": requirements.model_dump(by_alias=True, exclude_none=True),
        "payload": payload.payload,
        "resource": resource,
        "extensions": payload.extensions,
        "method": resource_method.upper() if resource_method else None,
    }
    if resource_identity is not None:
        normalized["resourceIdentity"] = resource_identity
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

from __future__ import annotations

import base64
import binascii
import json
import re
import string
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from xrpl_x402_middleware.exceptions import InvalidPaymentHeaderError

if TYPE_CHECKING:
    from xrpl_x402_middleware.types import XRPLAmount, XRPLAsset, XRPLPaymentOption


CAIP_2_NETWORK_PATTERN = re.compile(r"^xrpl:[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_HEX_DIGITS = frozenset(string.hexdigits)

ModelType = TypeVar("ModelType", bound=BaseModel)


def is_valid_xrpl_network(network: str) -> bool:
    return bool(CAIP_2_NETWORK_PATTERN.fullmatch(network))


def normalize_currency_code(currency: str) -> str:
    normalized = str(currency).strip().upper()
    if not normalized:
        raise ValueError("Asset code is required")

    if len(normalized) == 40 and set(normalized) <= _HEX_DIGITS:
        decoded = bytes.fromhex(normalized)
        decoded_ascii = decoded.rstrip(b"\x00")
        if decoded_ascii and all(32 <= byte <= 126 for byte in decoded_ascii):
            normalized = decoded_ascii.decode("ascii").upper()

    return normalized


def canonical_asset_identifier(asset: XRPLAsset) -> str:
    code = normalize_currency_code(asset.code)
    if asset.issuer is None:
        return f"{code}:native"
    return f"{code}:{asset.issuer.strip()}"


def build_xrpl_extra(asset: XRPLAsset, amount: XRPLAmount) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "asset": asset.model_dump(exclude_none=True),
        "assetId": canonical_asset_identifier(asset),
        "amount": amount.model_dump(by_alias=True, exclude_none=True),
    }
    return {"xrpl": payload}


def encode_model_to_base64(model: BaseModel) -> str:
    payload = model.model_dump_json(by_alias=True, exclude_none=True)
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def decode_model_from_base64(raw_value: str, model_type: type[ModelType]) -> ModelType:
    try:
        decoded = base64.b64decode(raw_value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidPaymentHeaderError("Header is not valid Base64") from exc

    try:
        decoded_json = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidPaymentHeaderError("Header is not valid UTF-8 JSON") from exc

    try:
        return TypeAdapter(model_type).validate_python(decoded_json)
    except ValidationError as exc:
        raise InvalidPaymentHeaderError("Header payload does not match the x402 schema") from exc


def payment_option_matches(
    option: XRPLPaymentOption,
    *,
    destination: str,
    asset: XRPLAsset,
    amount: XRPLAmount,
) -> bool:
    if option.pay_to != destination:
        return False

    if canonical_asset_identifier(option.asset) != canonical_asset_identifier(asset):
        return False

    if option.amount.unit != amount.unit:
        return False

    if option.amount.value != amount.value:
        return False

    if option.amount.drops != amount.drops:
        return False

    return True

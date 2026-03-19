from __future__ import annotations

import base64
import binascii
from decimal import Decimal
import json
import re
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from xrpl_x402_core.assets import asset_identifier_from_parts, normalize_currency_code

if TYPE_CHECKING:
    from xrpl_x402_core.models import StructuredAmount, XRPLAmount, XRPLAsset, XRPLPaymentOption


CAIP_2_NETWORK_PATTERN = re.compile(r"^xrpl:[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

ModelType = TypeVar("ModelType", bound=BaseModel)


def is_valid_xrpl_network(network: str) -> bool:
    return bool(CAIP_2_NETWORK_PATTERN.fullmatch(network))


def canonical_asset_identifier(asset: "XRPLAsset") -> str:
    return asset_identifier_from_parts(asset.code, asset.issuer)


def build_xrpl_extra(asset: "XRPLAsset", amount: "XRPLAmount") -> dict[str, Any]:
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
        raise ValueError("Header is not valid Base64") from exc

    try:
        decoded_json = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Header is not valid UTF-8 JSON") from exc

    try:
        return TypeAdapter(model_type).validate_python(decoded_json)
    except ValidationError as exc:
        raise ValueError("Header payload does not match the x402 schema") from exc


def payment_option_matches(
    option: "XRPLPaymentOption",
    *,
    destination: str,
    asset: "XRPLAsset",
    amount: "XRPLAmount",
) -> bool:
    if option.pay_to != destination:
        return False
    if canonical_asset_identifier(option.asset) != canonical_asset_identifier(asset):
        return False
    if option.amount.unit != amount.unit:
        return False
    if option.amount.drops != amount.drops:
        return False
    if option.amount.unit == "issued" and Decimal(option.amount.value) != Decimal(amount.value):
        return False
    if option.amount.unit != "issued" and option.amount.value != amount.value:
        return False
    return True


def amount_from_structured_amount(amount: "StructuredAmount") -> "XRPLAmount":
    from xrpl_x402_core.models import XRPLAmount

    return XRPLAmount(
        value=amount.value,
        unit=amount.unit,
        drops=amount.drops,
    )


def xrpl_asset_from_identifier(identifier: str) -> "XRPLAsset":
    from xrpl_x402_core.models import XRPLAsset

    asset = asset_identifier_from_parts(*_parse_identifier_parts(identifier))
    code, _, issuer = asset.partition(":")
    if issuer == "native":
        return XRPLAsset(code=code)
    return XRPLAsset(code=code, issuer=issuer)


def _parse_identifier_parts(identifier: str) -> tuple[str, str | None]:
    code, separator, issuer = identifier.partition(":")
    normalized_code = normalize_currency_code(code)
    if not separator:
        raise ValueError("Asset identifier must use CODE:ISSUER or CODE:native")
    normalized_issuer = issuer.strip()
    if normalized_issuer == "native":
        return normalized_code, None
    if not normalized_issuer:
        raise ValueError("Asset identifier issuer is required")
    return normalized_code, normalized_issuer

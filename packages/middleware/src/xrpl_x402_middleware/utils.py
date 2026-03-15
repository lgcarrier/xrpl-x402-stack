from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from xrpl_x402_core import (
    canonical_asset_identifier,
    decode_model_from_base64 as decode_model_from_base64_core,
    encode_model_to_base64,
    is_valid_xrpl_network,
    payment_option_matches,
)

from xrpl_x402_middleware.exceptions import InvalidPaymentHeaderError

ModelType = TypeVar("ModelType", bound=BaseModel)


def decode_model_from_base64(raw_value: str, model_type: type[ModelType]) -> ModelType:
    try:
        return decode_model_from_base64_core(raw_value, model_type)
    except ValueError as exc:
        raise InvalidPaymentHeaderError(str(exc)) from exc


__all__ = [
    "canonical_asset_identifier",
    "decode_model_from_base64",
    "encode_model_to_base64",
    "is_valid_xrpl_network",
    "payment_option_matches",
]

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from x402.schemas import AssetAmount, PaymentRequirements, SupportedKind
from x402.schemas.helpers import parse_money

from xrpl_x402_core import (
    XRP_CODE,
    get_default_asset,
    xrpl_currency_code,
    validate_requirements_shape,
)

ServerT = TypeVar("ServerT")


class ExactXRPLServerScheme:
    """Resource-server half of the x402 v2 exact XRPL mechanism."""

    scheme = "exact"
    default_asset_transfer_method = "sequence"
    payment_flows = {
        "sequence": {
            "supported": ["authorization"],
            "default": "authorization",
        },
        "ticketSequence": {
            "supported": ["authorization"],
            "default": "authorization",
        },
    }

    def parse_price(self, price: Any, network: str) -> AssetAmount:
        if isinstance(price, AssetAmount):
            return _validate_asset_amount(price)
        if isinstance(price, dict) and "amount" in price:
            return _validate_asset_amount(AssetAmount.model_validate(price))

        parsed = parse_money(price)
        default = get_default_asset(str(network), parsed.get("symbol"))
        return AssetAmount(
            amount=_decimal_string(parsed["amount"]),
            asset=str(default["asset"]),
            extra={
                "issuer": str(default["issuer"]),
                "areFeesSponsored": False,
                "assetTransferMethod": "sequence",
            },
        )

    def get_asset_decimals(self, asset: str, network: str) -> int | None:
        del asset, network
        # XRPL IOUs use decimal ledger values directly; there is no protocol
        # token-decimals conversion for dynamic or settlement amounts.
        return None

    def enhance_payment_requirements(
        self,
        requirements: PaymentRequirements,
        supported_kind: SupportedKind,
        extension_keys: list[str],
    ) -> PaymentRequirements:
        del extension_keys
        merged = dict(supported_kind.extra or {})
        merged.update(requirements.extra or {})
        merged["areFeesSponsored"] = False
        methods = merged.pop("assetTransferMethods", None)
        merged.pop("defaultAssetTransferMethod", None)
        merged.pop("paymentFlows", None)
        method = merged.get("assetTransferMethod") or "sequence"
        if methods and method not in methods:
            raise ValueError(
                f"XRPL transfer method {method} is not supported by the facilitator"
            )
        merged["assetTransferMethod"] = method
        enhanced = requirements.model_copy(update={"extra": merged})
        validate_requirements_shape(enhanced)
        return enhanced


def register_exact_xrpl_server(
    server: ServerT,
    networks: str | list[str] | None = None,
) -> ServerT:
    selected = (
        [networks]
        if isinstance(networks, str)
        else networks or ["xrpl:0", "xrpl:1", "xrpl:2"]
    )
    scheme = ExactXRPLServerScheme()
    for network in selected:
        server.register(network, scheme)
    return server


def _validate_asset_amount(value: AssetAmount) -> AssetAmount:
    asset = xrpl_currency_code(value.asset)
    extra = dict(value.extra or {})
    extra["areFeesSponsored"] = False
    extra.setdefault("assetTransferMethod", "sequence")
    if asset == XRP_CODE:
        if extra.get("issuer") is not None:
            raise ValueError("XRP prices must not include an issuer")
        if not str(value.amount).isdigit():
            raise ValueError("XRP prices use an integer drops string")
    elif not extra.get("issuer"):
        raise ValueError("XRPL IOU prices require extra.issuer")
    return AssetAmount(amount=str(value.amount), asset=asset, extra=extra)


def _decimal_string(value: str) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("XRPL IOU price must be a decimal string") from exc
    if parsed <= 0:
        raise ValueError("XRPL exact price must be greater than zero")
    rendered = format(parsed, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


__all__ = ["ExactXRPLServerScheme", "register_exact_xrpl_server"]

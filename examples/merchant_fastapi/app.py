from __future__ import annotations

import os

from fastapi import FastAPI, Request

from xrpl_x402_middleware import PaymentMiddlewareASGI, RouteConfig, require_payment

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "xrpl:1"
DEFAULT_PRICE_DROPS = 1000


def facilitator_url_from_env() -> str:
    return os.getenv("FACILITATOR_URL", DEFAULT_FACILITATOR_URL)


def facilitator_token_from_env() -> str:
    return os.getenv("FACILITATOR_TOKEN", DEFAULT_FACILITATOR_TOKEN)


def merchant_xrpl_address_from_env() -> str:
    return os.getenv("MERCHANT_XRPL_ADDRESS", DEFAULT_MERCHANT_XRPL_ADDRESS)


def xrpl_network_from_env() -> str:
    return os.getenv("XRPL_NETWORK", DEFAULT_XRPL_NETWORK)


def price_drops_from_env() -> int:
    return int(os.getenv("PRICE_DROPS", str(DEFAULT_PRICE_DROPS)))


def price_asset_code_from_env() -> str:
    return os.getenv("PRICE_ASSET_CODE", "XRP").strip().upper() or "XRP"


def price_asset_issuer_from_env() -> str | None:
    return os.getenv("PRICE_ASSET_ISSUER", "").strip() or None


def price_asset_amount_from_env() -> str | None:
    return os.getenv("PRICE_ASSET_AMOUNT", "").strip() or None


def build_premium_route_config() -> RouteConfig:
    facilitator_url = facilitator_url_from_env()
    facilitator_token = facilitator_token_from_env()
    merchant_xrpl_address = merchant_xrpl_address_from_env()
    xrpl_network = xrpl_network_from_env()
    price_drops = price_drops_from_env()
    price_asset_code = price_asset_code_from_env()
    price_asset_issuer = price_asset_issuer_from_env()
    price_asset_amount = price_asset_amount_from_env()

    uses_issued_asset = (
        price_asset_code != "XRP"
        or price_asset_issuer is not None
        or price_asset_amount is not None
    )
    if not uses_issued_asset:
        return require_payment(
            facilitator_url=facilitator_url,
            bearer_token=facilitator_token,
            pay_to=merchant_xrpl_address,
            network=xrpl_network,
            xrp_drops=price_drops,
            description="One premium XRPL x402 request",
        )

    if price_asset_code == "XRP":
        raise RuntimeError(
            "Issued-asset pricing requires PRICE_ASSET_CODE to be set to a non-XRP asset"
        )
    if price_asset_issuer is None:
        raise RuntimeError("Issued-asset pricing requires PRICE_ASSET_ISSUER")
    if price_asset_amount is None:
        raise RuntimeError("Issued-asset pricing requires PRICE_ASSET_AMOUNT")

    return require_payment(
        facilitator_url=facilitator_url,
        bearer_token=facilitator_token,
        pay_to=merchant_xrpl_address,
        network=xrpl_network,
        amount=price_asset_amount,
        asset_code=price_asset_code,
        asset_issuer=price_asset_issuer,
        description=f"One premium {price_asset_code} XRPL x402 request",
    )


def create_app(*, client_factory=None) -> FastAPI:
    app = FastAPI(title="XRPL x402 Merchant Example")
    middleware_kwargs = {}
    if client_factory is not None:
        middleware_kwargs["client_factory"] = client_factory

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"GET /premium": build_premium_route_config()},
        **middleware_kwargs,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/premium")
    async def premium(request: Request) -> dict[str, str]:
        payment = request.state.x402_payment
        return {
            "message": "premium content unlocked",
            "payer": payment.payer,
            "invoice_id": payment.invoice_id,
            "tx_hash": payment.tx_hash,
        }

    return app


app = create_app()

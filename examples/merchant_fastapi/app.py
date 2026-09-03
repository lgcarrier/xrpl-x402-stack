from __future__ import annotations

import os

from fastapi import FastAPI

from xrpl_x402_middleware import PaymentMiddlewareASGI, RouteConfig, require_payment

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "xrpl:1"


def build_premium_route_config() -> RouteConfig:
    asset = os.getenv("PRICE_ASSET_CODE", "XRP").strip().upper() or "XRP"
    issuer = os.getenv("PRICE_ASSET_ISSUER", "").strip() or None
    common = {
        "pay_to": os.getenv(
            "MERCHANT_XRPL_ADDRESS", DEFAULT_MERCHANT_XRPL_ADDRESS
        ),
        "network": os.getenv("XRPL_NETWORK", DEFAULT_XRPL_NETWORK),
        "resource": "https://merchant.example/premium",
        "description": "One premium XRPL x402 request",
        "mime_type": "application/json",
        "service_name": "XRPL x402 merchant",
        "tags": ["xrpl", "premium"],
        "icon_url": "https://xrpl.org/assets/favicon-32x32.png",
    }
    if asset == "XRP":
        if issuer is not None:
            raise RuntimeError("XRP pricing must not configure an issuer")
        return require_payment(
            xrp_drops=int(os.getenv("PRICE_DROPS", "1000")),
            **common,
        )
    amount = os.getenv("PRICE_ASSET_AMOUNT", "").strip()
    if not amount or issuer is None:
        raise RuntimeError("IOU pricing requires PRICE_ASSET_AMOUNT and PRICE_ASSET_ISSUER")
    return require_payment(
        amount=amount,
        asset=asset,
        issuer=issuer,
        **common,
    )


def create_app(*, facilitator_client=None) -> FastAPI:
    app = FastAPI(title="XRPL x402 Merchant Example")
    middleware_config = {
        "routes": {"GET /premium": build_premium_route_config()},
    }
    if facilitator_client is None:
        middleware_config.update(
            facilitator_url=os.getenv("FACILITATOR_URL", DEFAULT_FACILITATOR_URL),
            bearer_token=os.getenv("FACILITATOR_TOKEN", DEFAULT_FACILITATOR_TOKEN),
        )
    else:
        middleware_config["facilitator_client"] = facilitator_client
    app.add_middleware(PaymentMiddlewareASGI, **middleware_config)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/premium")
    async def premium() -> dict[str, str]:
        # Settlement happens after this handler succeeds. The middleware buffers
        # these bytes and releases them only after validated tesSUCCESS.
        return {"message": "premium content unlocked"}

    return app


app = create_app()

from __future__ import annotations

import os

from fastapi import FastAPI, Request

from xrpl_x402_middleware import PaymentMiddlewareASGI, require_payment

FACILITATOR_URL = os.getenv("FACILITATOR_URL", "http://127.0.0.1:8000")
FACILITATOR_TOKEN = os.getenv("FACILITATOR_TOKEN", "replace-with-your-facilitator-token")
MERCHANT_XRPL_ADDRESS = os.getenv(
    "MERCHANT_XRPL_ADDRESS",
    "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
)
XRPL_NETWORK = os.getenv("XRPL_NETWORK", "xrpl:1")
PRICE_DROPS = int(os.getenv("PRICE_DROPS", "1000"))

app = FastAPI(title="XRPL x402 Merchant Example")
app.add_middleware(
    PaymentMiddlewareASGI,
    route_configs={
        "GET /premium": require_payment(
            facilitator_url=FACILITATOR_URL,
            bearer_token=FACILITATOR_TOKEN,
            pay_to=MERCHANT_XRPL_ADDRESS,
            network=XRPL_NETWORK,
            xrp_drops=PRICE_DROPS,
            description="One premium XRPL x402 request",
        )
    },
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

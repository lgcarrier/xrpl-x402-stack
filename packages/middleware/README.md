# xrpl-x402-middleware

`xrpl-x402-middleware` protects ASGI and FastAPI routes with exact XRPL x402 payments.

## Install

```bash
pip install xrpl-x402-middleware
```

Optional Coinbase Python `x402` interop:

```bash
pip install "xrpl-x402-middleware[x402]"
```

## Quick Start

```python
from fastapi import FastAPI, Request

from xrpl_x402_middleware import PaymentMiddlewareASGI, require_payment

app = FastAPI()
app.add_middleware(
    PaymentMiddlewareASGI,
    route_configs={
        "GET /premium": require_payment(
            facilitator_url="http://127.0.0.1:8000",
            bearer_token="replace-with-your-token",
            pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
            network="xrpl:1",
            xrp_drops=1000,
            description="One premium request",
        )
    },
)

@app.get("/premium")
async def premium(request: Request) -> dict[str, str]:
    payment = request.state.x402_payment
    return {"payer": payment.payer, "tx_hash": payment.tx_hash}
```

## Public API

- `PaymentMiddlewareASGI`
- `require_payment(...)`
- `XRPLFacilitatorClient`
- Optional adapters under `xrpl_x402_middleware.adapters.x402`

## Compatibility

- Python `3.12`
- Depends on `xrpl-x402-core`
- Optional adapter extra pins `x402==2.3.0`
- Examples target `xrpl:1`; mainnet usage uses `xrpl:0`

## Provenance

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

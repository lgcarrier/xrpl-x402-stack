# xrpl-x402-middleware

[![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-middleware?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-middleware/)
[![Source directory](https://img.shields.io/badge/GitHub-packages%2Fmiddleware-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack/tree/main/packages/middleware)

Install:

```bash
pip install xrpl-x402-middleware
```

Optional upstream `x402` interop:

```bash
pip install "xrpl-x402-middleware[x402]"
```

## Public API

- `PaymentMiddlewareASGI`
- `require_payment(...)`
- `XRPLFacilitatorClient`

## Quickstart With The Example Merchant

Install the package:

```bash
pip install xrpl-x402-middleware
```

Point the example merchant at your facilitator and merchant wallet:

```bash
export FACILITATOR_URL=http://127.0.0.1:8000
export FACILITATOR_TOKEN=replace-with-your-facilitator-token
export MERCHANT_XRPL_ADDRESS=rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe
export XRPL_NETWORK=xrpl:1
export PRICE_DROPS=1000
```

Run the repo example:

```bash
uvicorn examples.merchant_fastapi.app:app --reload --port 8010
```

Then request `GET /premium`. The first request will return `402 Payment Required`, and a buyer that retries with a valid payment will receive `200`.

The example route handler reads the verified payment from `request.state.x402_payment`.

That object includes the resolved `invoice_id`, so seller code can log or persist the exact request/payment binding that the facilitator accepted.

Example:

```python
from fastapi import FastAPI, Request


@app.get("/premium")
async def premium(request: Request) -> dict[str, str]:
    payment = request.state.x402_payment
    return {
        "payer": payment.payer,
        "invoice_id": payment.invoice_id,
        "tx_hash": payment.tx_hash,
    }
```

## Minimal FastAPI Integration

```python
from fastapi import FastAPI

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
        )
    },
)
```

## Price XRP Or Issued Assets

XRP pricing uses `xrp_drops`:

```python
require_payment(
    facilitator_url="http://127.0.0.1:8000",
    bearer_token="replace-with-your-token",
    pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
    network="xrpl:1",
    xrp_drops=1000,
)
```

Issued-asset pricing uses `amount`, `asset_code`, and `asset_issuer`:

```python
require_payment(
    facilitator_url="http://127.0.0.1:8000",
    bearer_token="replace-with-your-token",
    pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
    network="xrpl:1",
    amount="1.25",
    asset_code="RLUSD",
    asset_issuer="rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV",
)
```

The repo example also supports env-driven issued-asset pricing:

```bash
export PRICE_ASSET_CODE=RLUSD
export PRICE_ASSET_ISSUER=rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV
export PRICE_ASSET_AMOUNT=1.25
```

## Invoice ID Behavior

The middleware does not generate invoice IDs on its own. It forwards the buyer-provided `invoice_id` to the facilitator during verify and settle, then exposes the facilitator-resolved value back to the app.

That means:

- if the buyer signs a payment with XRPL `InvoiceID`, your route can trust `request.state.x402_payment.invoice_id`
- if the buyer omits it, your route still gets a stable fallback value derived by the facilitator
- if the buyer sends a mismatched payload `invoice_id`, the facilitator rejects the payment before your route runs

For the full asset flows, continue to the [RLUSD guide](../asset-guides/rlusd.md) or [USDC guide](../asset-guides/usdc.md).

## Header And Adapter Details

The middleware emits a Base64-encoded `PAYMENT-REQUIRED` challenge header on `402`, accepts a Base64-encoded `PAYMENT-SIGNATURE` header on retry, and adds a Base64-encoded `PAYMENT-RESPONSE` header to successful responses.

See [Header Contract](../how-it-works/header-contract.md) for the exact shapes, [Replay And Freshness](../how-it-works/replay-and-freshness.md) for the facilitator-side replay rules that sit behind settlement, and [Coinbase x402 Adapters](../integrations/x402-adapters.md) if you want to plug the middleware into the upstream Python `x402` server abstraction.

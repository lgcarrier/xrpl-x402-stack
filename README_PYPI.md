# xrpl-x402-middleware

`xrpl-x402-middleware` is a Python ASGI middleware package for protecting API
routes with XRPL-backed x402 payments.

It is published from the
[XRPL x402 facilitator repository](https://github.com/lgcarrier/xrpl-x402-facilitator)
and pairs with the facilitator service in that repo for payment verification
and settlement.

## Install

```bash
pip install xrpl-x402-middleware
```

Import it in Python as:

```python
from xrpl_x402_middleware import PaymentMiddlewareASGI, require_payment
```

## What It Does

- Returns `402 Payment Required` challenges with a Base64-encoded
  `PAYMENT-REQUIRED` header.
- Accepts Base64-encoded `PAYMENT-SIGNATURE` headers that carry an x402 v2
  payment payload with an XRPL signed transaction blob.
- Calls an XRPL facilitator for `/verify` and `/settle`.
- Compares the verified XRPL destination, asset, and amount against exact route
  pricing.
- Injects settled payment context onto `request.state.x402_payment`.
- Adds a Base64-encoded `PAYMENT-RESPONSE` header to successful paid responses.

## Supported Networks

- `xrpl:0` for XRPL mainnet
- `xrpl:1` for XRPL testnet

## FastAPI Example

```python
from fastapi import FastAPI

from xrpl_x402_middleware import PaymentMiddlewareASGI, require_payment

app = FastAPI()
app.add_middleware(
    PaymentMiddlewareASGI,
    route_configs={
        "POST /premium": require_payment(
            facilitator_url="http://127.0.0.1:8000",
            bearer_token="replace-with-your-facilitator-token",
            pay_to="rYourXRPLReceivingAddressHere1234567890...",
            network="xrpl:1",
            xrp_drops=1000,
            description="One premium API call",
        )
    },
)
```

## Payment Header Shape

Decoded `PAYMENT-SIGNATURE` payload:

```json
{
  "x402Version": 2,
  "scheme": "exact",
  "network": "xrpl:1",
  "payload": {
    "signedTxBlob": "120000...",
    "invoiceId": "optional-signed-invoice-id"
  }
}
```

## Source And Docs

- Repository:
  [github.com/lgcarrier/xrpl-x402-facilitator](https://github.com/lgcarrier/xrpl-x402-facilitator)
- Full repo documentation:
  [README.md](https://github.com/lgcarrier/xrpl-x402-facilitator/blob/main/README.md)

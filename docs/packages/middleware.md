# xrpl-x402-middleware

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

## Example

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

The middleware supports both XRP and issued-asset route pricing through `require_payment(...)`.

# Coinbase x402 Adapters

The stack can register XRPL exact-payment behavior with the Coinbase Python `x402` SDK without taking `x402` as a hard runtime dependency.

Install the extras only when you need that interop:

```bash
pip install "xrpl-x402-client[x402]" "xrpl-x402-middleware[x402]"
```

The compatibility target for this stack is `x402==2.3.0`.

## Client Adapter

Use `register_exact_xrpl_client(...)` to teach an upstream `x402` client how to build XRPL exact-payment payloads with `XRPLPaymentSigner`.

```python
from x402 import x402ClientSync
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner
from xrpl_x402_client.adapters.x402 import register_exact_xrpl_client

signer = XRPLPaymentSigner(
    Wallet.create(),
    network="xrpl:1",
    autofill_enabled=False,
)

client = register_exact_xrpl_client(x402ClientSync(), signer)
```

By default, the adapter registers the scheme for `xrpl:*`. You can narrow that with `networks="xrpl:1"` or a list of specific XRPL network identifiers.

The adapter converts upstream `PaymentRequirements` into the same XRPL `signedTxBlob` payload that `xrpl-x402-client` would send directly.

## Server Adapter

Use `register_exact_xrpl_server(...)` to add XRPL exact-payment support to an upstream `x402` resource server backed by a facilitator.

```python
from x402 import x402ResourceServer

from xrpl_x402_middleware.adapters.x402 import (
    XRPLX402FacilitatorClient,
    register_exact_xrpl_server,
)

facilitator_client = XRPLX402FacilitatorClient(
    base_url="https://facilitator.example",
    bearer_token="replace-with-your-token",
)
server = register_exact_xrpl_server(x402ResourceServer(facilitator_client))
server.initialize()
```

The adapter registers the XRPL exact-price scheme for `xrpl:*` by default, or for the provided `networks` list when you want tighter scoping.

## What The Server Adapter Surfaces

The adapter pulls `GET /supported` from the facilitator and exposes that data through upstream `SupportedResponse`.

Example `SupportedKind.extra` payload:

```json
{
  "xrpl": {
    "assets": [
      "XRP:native",
      "RLUSD:rnEVYfAWYP5HpPaWQiPSJMyDeUiEJ6zhy2",
      "USDC:rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt"
    ],
    "settlementMode": "validated"
  }
}
```

That gives upstream `x402` callers enough information to discover:

- which XRPL network the facilitator serves
- which exact assets are currently supported
- whether settlement is `validated` or `optimistic`

## Verify And Settle Semantics

When the upstream `x402` server verifies or settles a payment, the adapter forwards:

- `signed_tx_blob`
- optional `invoice_id`

to the facilitator `/verify` and `/settle` endpoints.

HTTP `401` and `402` responses are converted into upstream `x402` invalid-payment or failed-settlement responses. Other non-2xx responses are treated as transport failures.

## When To Use The Adapters

Use the adapters when you already have upstream Python `x402` abstractions in your app and want XRPL exact-payment support without rewriting those integration points.

Use the native `xrpl-x402-middleware` and `xrpl-x402-client` APIs when you want the most direct Python-first XRPL integration and the smallest dependency surface.

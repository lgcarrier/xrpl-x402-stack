# xrpl-x402-client

`xrpl-x402-client` is the buyer-side Python SDK for the Open XRPL x402 Stack.

## Install

```bash
pip install xrpl-x402-client
```

Optional Coinbase Python `x402` interop:

```bash
pip install "xrpl-x402-client[x402]"
```

## Quick Start

```python
import asyncio

from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner, wrap_httpx_with_xrpl_payment

wallet = Wallet.create()
signer = XRPLPaymentSigner(
    wallet,
    network="xrpl:1",
    autofill_enabled=False,
)

async def fetch_paid_resource() -> None:
    async with wrap_httpx_with_xrpl_payment(
        signer,
        base_url="https://merchant.example",
    ) as client:
        response = await client.get("/premium")
        print(response.status_code, response.text)

asyncio.run(fetch_paid_resource())
```

## Public API

- `decode_payment_required(...)`
- `select_payment_option(...)`
- `build_payment_signature(...)`
- `XRPLPaymentSigner`
- `XRPLPaymentTransport`
- `wrap_httpx_with_xrpl_payment(...)`
- Optional adapters under `xrpl_x402_client.adapters.x402`

## Compatibility

- Python `3.12`
- `xrpl-py==4.0.0`
- Optional adapter extra pins `x402==2.3.0`
- Examples target `xrpl:1`; mainnet usage uses `xrpl:0`

## Provenance

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

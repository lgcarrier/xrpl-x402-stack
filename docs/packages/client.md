# xrpl-x402-client

[![PyPI package](https://img.shields.io/badge/PyPI-xrpl--x402--client-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-client/)
[![Source directory](https://img.shields.io/badge/GitHub-packages%2Fclient-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack/tree/main/packages/client)

Install:

```bash
pip install xrpl-x402-client
```

Optional upstream `x402` interop:

```bash
pip install "xrpl-x402-client[x402]"
```

## Public API

- `decode_payment_required(...)`
- `select_payment_option(...)`
- `build_payment_signature(...)`
- `XRPLPaymentSigner`
- `XRPLPaymentTransport`
- `wrap_httpx_with_xrpl_payment(...)`

## Quickstart With The Example Buyer

Install the package:

```bash
pip install xrpl-x402-client
```

Point the buyer at a funded Testnet wallet and a protected route:

```bash
export XRPL_WALLET_SEED=sEd...
export XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
export XRPL_NETWORK=xrpl:1
export TARGET_URL=http://127.0.0.1:8010/premium
```

Run the repo example:

```bash
python examples/buyer_httpx.py
```

Expected output:

```text
status=200
{"message":"premium content unlocked", ...}
```

## Add An Invoice ID

If you want a stable request identifier, pass an `invoice_id_factory` into the `httpx` wrapper:

```python
import hashlib
import time

from xrpl_x402_client import wrap_httpx_with_xrpl_payment


def invoice_id_factory() -> str:
    return hashlib.sha256(f"buyer:{time.time_ns()}".encode()).hexdigest().upper()[:32]


client = wrap_httpx_with_xrpl_payment(
    signer,
    invoice_id_factory=invoice_id_factory,
)
```

When you do this, the client writes the same value into the signed XRPL transaction `InvoiceID` and into the x402 retry payload.

If you omit `invoice_id_factory`, the payment still works. The facilitator will derive a fallback invoice ID from the signed blob hash.

## Choose The Payment Asset

The buyer defaults to XRP unless you set `PAYMENT_ASSET`.

Use XRP:

```bash
unset PAYMENT_ASSET
```

Use RLUSD:

```bash
export PAYMENT_ASSET=RLUSD:rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV
```

Use USDC:

```bash
export PAYMENT_ASSET=USDC:rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt
```

The selected asset must match one of the payment options challenged by the merchant. The repo merchant example can be switched with `PRICE_ASSET_CODE`, `PRICE_ASSET_ISSUER`, and `PRICE_ASSET_AMOUNT`.

## Minimal SDK Integration

```python
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner, wrap_httpx_with_xrpl_payment

wallet = Wallet.create()
signer = XRPLPaymentSigner(wallet, network="xrpl:1", autofill_enabled=False)
```

The buyer example supports explicit asset selection through `PAYMENT_ASSET`, including `XRP:native`, `RLUSD:<issuer>`, and `USDC:<issuer>`.

## Header And Adapter Details

The client decodes `PAYMENT-REQUIRED` from the response header when available and falls back to the `402` JSON body for compatibility. It then sends the signed retry in `PAYMENT-SIGNATURE` and reads the final facilitator result from `PAYMENT-RESPONSE`.

See [Header Contract](../how-it-works/header-contract.md) for the exact wire shape, [Payment Flow](../how-it-works/payment-flow.md) for the end-to-end lifecycle, and [Coinbase x402 Adapters](../integrations/x402-adapters.md) if you want to register the XRPL signer with the upstream Python `x402` client abstraction.

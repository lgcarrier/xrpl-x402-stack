# xrpl-x402-client

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

## Example

```python
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner, wrap_httpx_with_xrpl_payment

wallet = Wallet.create()
signer = XRPLPaymentSigner(wallet, network="xrpl:1", autofill_enabled=False)
```

The buyer example also supports explicit asset selection through `PAYMENT_ASSET`, including `XRP:native`, `RLUSD:<issuer>`, and `USDC:<issuer>`.

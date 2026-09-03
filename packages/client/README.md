# xrpl-x402-client

Client mechanism for x402 v2 exact XRPL payments.

```python
from x402 import x402Client
from xrpl_x402_client import register_exact_xrpl_client

client = register_exact_xrpl_client(x402Client(), signer)
client.set_spend_controls({"max_amount_per_payment": "$1"})
```

`ExactXRPLClientScheme` constructs and signs sequence or ticket payments,
derives `LastLedgerSequence` from the timeout, binds custom networks through
`NetworkID`, hashes advertised invoice IDs, and emits only
`{"signedTxBlob": "..."}` as the scheme payload.

Use `XRPLAssetSpendLimit` for XRP, USDC, or custom IOUs. These assets are not
silently authorized by the default pegged-asset policy.

The `XRPLPaymentTransport` and `wrap_httpx_with_xrpl_payment` helpers use the
official `x402AsyncTransport`, which retries a replayable protected request at
most once.
Custom async streaming bodies are sent exactly once and a `402` is returned to
the caller without buffering, signing, or automatic retry.

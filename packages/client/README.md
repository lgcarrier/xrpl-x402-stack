# xrpl-x402-client

`xrpl-x402-client` is the buyer-side Python SDK for the Open XRPL x402 Stack.

It includes:

- Challenge decoding and payment option selection
- Exact XRPL payment signing for XRP and issued assets
- Base64 `PAYMENT-SIGNATURE` construction
- Async `httpx` retry helpers for one automatic `402` retry
- Optional `x402` client adapters under `xrpl_x402_client.adapters`

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

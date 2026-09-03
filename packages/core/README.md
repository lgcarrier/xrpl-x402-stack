# xrpl-x402-core

Shared XRPL support for the x402 v2 stack. Version 0.2.0 pins `x402==2.21.0`.

This package re-exports official x402 schemas such as `PaymentRequired`,
`PaymentRequirements`, `PaymentPayload`, `VerifyRequest`, `VerifyResponse`,
`SettleRequest`, `SettleResponse`, and supported-kind models. It does not define
parallel wire schemas.

Local public types are XRPL-specific:

- `ExactXRPLPayload` with `signedTxBlob`
- `ExactXRPLExtra`
- `XRPLSettlementState`
- default asset, network, invoice hashing, transaction hash, and fingerprint helpers

XRPL assets are represented canonically: XRP is `XRP` with drops, while IOUs
use the currency code plus `extra.issuer`. `find_default_asset(asset, network)`
recognizes the official RLUSD entries.

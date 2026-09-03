# Core package

`xrpl-x402-core` re-exports official x402 2.20.0 protocol schemas. The only
locally owned models are XRPL-specific payload/extra/state records.

Important exports include:

- `PaymentRequired`, `PaymentRequirements`, `PaymentPayload`
- `VerifyRequest`, `VerifyResponse`, `SettleRequest`, `SettleResponse`
- `ExactXRPLPayload`, `ExactXRPLExtra`, `XRPLSettlementState`
- `find_default_asset`, invoice hashing, network and fingerprint helpers

XRP is represented by `asset: "XRP"` with drops. IOUs use their currency code
and `extra.issuer`; decimals are not a protocol field.

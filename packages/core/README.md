# xrpl-x402-core

Shared XRPL/x402 wire models, codecs, validation helpers, and exact-payment utilities for the Open XRPL x402 Stack.

This package is the source of truth for:

- `PaymentRequired`, `PaymentPayload`, and `PaymentResponse`
- `XRPLAsset`, `XRPLAmount`, and `XRPLPaymentOption`
- Base64 header encoding and decoding helpers
- CAIP-2 `xrpl:*` validation
- XRPL asset normalization and exact-price matching

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

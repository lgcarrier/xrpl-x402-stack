# xrpl-x402-core

Install:

```bash
pip install xrpl-x402-core
```

Use `xrpl-x402-core` when you need the shared XRPL/x402 models and helpers directly.

## Main Exports

- `PaymentRequired`
- `PaymentPayload`
- `PaymentResponse`
- `XRPLAsset`
- `XRPLAmount`
- `XRPLPaymentOption`
- `encode_model_to_base64(...)`
- `decode_model_from_base64(...)`
- `payment_option_matches(...)`

## What It Owns

- the canonical wire models shared by the facilitator, middleware, and client packages
- Base64 header codecs for `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and `PAYMENT-RESPONSE`
- XRPL asset normalization and exact-payment matching
- facilitator request and response models used on the wire

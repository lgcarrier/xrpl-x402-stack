# xrpl-x402-core

[![PyPI package](https://img.shields.io/badge/PyPI-xrpl--x402--core-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-core/)
[![Source directory](https://img.shields.io/badge/GitHub-packages%2Fcore-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack/tree/main/packages/core)

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

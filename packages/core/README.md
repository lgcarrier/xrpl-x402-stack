# xrpl-x402-core

Shared XRPL/x402 wire models, codecs, validation helpers, and exact-payment utilities for the Open XRPL x402 Stack.

## Install

```bash
pip install xrpl-x402-core
```

## Quick Start

```python
from xrpl_x402_core import (
    PaymentRequired,
    XRPLAmount,
    XRPLAsset,
    XRPLPaymentOption,
    decode_model_from_base64,
    encode_model_to_base64,
)

challenge = PaymentRequired(
    error="Payment required",
    accepts=[
        XRPLPaymentOption(
            network="xrpl:1",
            payTo="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
            maxAmountRequired="1000",
            asset=XRPLAsset(code="XRP"),
            amount=XRPLAmount(value="1000", unit="drops", drops=1000),
        )
    ],
)

header_value = encode_model_to_base64(challenge)
parsed = decode_model_from_base64(header_value, PaymentRequired)
assert parsed.accepts[0].network == "xrpl:1"
```

## Public API

- Shared x402 wire models: `PaymentRequired`, `PaymentPayload`, `PaymentResponse`
- XRPL payment models: `XRPLAsset`, `XRPLAmount`, `XRPLPaymentOption`, `XRPLPaymentPayload`
- Facilitator contract models: `PaymentRequest`, `FacilitatorSupportedResponse`, `FacilitatorVerifyResponse`, `FacilitatorSettleResponse`
- Header and matching helpers: `encode_model_to_base64`, `decode_model_from_base64`, `payment_option_matches`, `canonical_asset_identifier`
- Asset helpers and constants: `parse_asset_identifier`, `supported_asset_keys`, `XRP_CODE`, `RLUSD_*`, `USDC_*`

## Compatibility

- Python `3.12`
- Wire-level validation accepts CAIP-2 `xrpl:*` network identifiers
- Built-in asset helpers and examples cover `xrpl:0` and `xrpl:1`
- No `x402` dependency is required for this package

## Provenance

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

# Payment Flow

The stack uses exact-pay-per-request XRPL transactions.

## Request Lifecycle

1. The buyer requests a protected route.
2. The middleware returns `402 Payment Required` with a `PAYMENT-REQUIRED` challenge.
3. The buyer decodes the challenge and selects a matching exact-payment option.
4. The buyer signs a presigned XRPL payment and retries with `PAYMENT-SIGNATURE`.
5. The facilitator verifies and settles the transaction.
6. The middleware injects `request.state.x402_payment`, forwards the request, and adds `PAYMENT-RESPONSE` to the successful response.

## Main Components

- `xrpl-x402-client` handles challenge decoding, payment selection, signing, and retry logic.
- `xrpl-x402-middleware` guards seller routes and enforces exact-price matches.
- `xrpl-x402-facilitator` verifies and settles the signed XRPL transaction.
- `xrpl-x402-core` provides the shared wire models and helpers used by all three.

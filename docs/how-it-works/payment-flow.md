# Payment Flow

The stack uses exact-pay-per-request XRPL transactions.

## Request Lifecycle

1. The buyer requests a protected route.
2. The middleware returns `402 Payment Required` with a `PAYMENT-REQUIRED` challenge.
3. The buyer decodes the challenge and selects a matching exact-payment option.
4. The buyer signs a presigned XRPL payment and retries with `PAYMENT-SIGNATURE`.
5. The facilitator verifies and settles the transaction.
6. The middleware injects `request.state.x402_payment`, forwards the request, and adds `PAYMENT-RESPONSE` to the successful response.

## Invoice IDs

`invoice_id` is the request-level correlation and replay-protection identifier for a payment attempt.

The buyer may include it when constructing the signed payment:

- in the XRPL transaction `InvoiceID`
- in the x402 retry payload as `invoiceId`

The facilitator resolves the canonical value like this:

1. if the signed XRPL transaction includes `InvoiceID`, that value is authoritative
2. if the request payload also includes `invoice_id`, it must exactly match the transaction `InvoiceID`
3. if neither is present, the facilitator falls back to the first 32 hex characters of the signed transaction blob hash

That resolved `invoice_id` is then used for replay protection during verify and settle, and the middleware exposes it back to the app in `request.state.x402_payment.invoice_id` and in the `PAYMENT-RESPONSE` header.

In practice:

- use an explicit `invoice_id` when you want stable request correlation across client, facilitator, and app logs
- omit it when you are fine with the facilitator deriving one from the signed blob
- never send a payload `invoice_id` that differs from the on-ledger XRPL `InvoiceID`

## Main Components

- `xrpl-x402-client` handles challenge decoding, payment selection, signing, and retry logic.
- `xrpl-x402-middleware` guards seller routes and enforces exact-price matches.
- `xrpl-x402-facilitator` verifies and settles the signed XRPL transaction.
- `xrpl-x402-core` provides the shared wire models and helpers used by all three.

For the exact header payloads, continue to [Header Contract](header-contract.md). For Redis replay markers, `LastLedgerSequence` checks, and settlement timing rules, continue to [Replay And Freshness](replay-and-freshness.md).

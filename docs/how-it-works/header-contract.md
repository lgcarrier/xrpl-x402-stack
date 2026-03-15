# Header Contract

The buyer and seller packages speak x402 over three HTTP headers:

- `PAYMENT-REQUIRED`
- `PAYMENT-SIGNATURE`
- `PAYMENT-RESPONSE`

Header names are case-insensitive on the wire, but the stack documents them in uppercase.

## Encoding

Each header value is Base64-encoded UTF-8 JSON.

The shared helpers in `xrpl-x402-core` are:

- `encode_model_to_base64(...)`
- `decode_model_from_base64(...)`

If the client does not find a valid `PAYMENT-REQUIRED` header, it can fall back to the `402` response body, which mirrors the same challenge as plain JSON.

## `PAYMENT-REQUIRED`

This header is returned with `402 Payment Required`.

Decoded example:

```json
{
  "x402Version": 2,
  "error": "Payment required",
  "accepts": [
    {
      "scheme": "exact",
      "network": "xrpl:1",
      "payTo": "rMerchantAddress...",
      "maxAmountRequired": "1000",
      "asset": {"code": "XRP"},
      "amount": {
        "value": "1000",
        "unit": "drops",
        "drops": 1000
      },
      "description": "Premium content",
      "mimeType": "application/json",
      "extra": {
        "xrpl": {
          "asset": {"code": "XRP"},
          "assetId": "XRP:native",
          "amount": {
            "value": "1000",
            "unit": "drops",
            "drops": 1000
          }
        }
      }
    }
  ]
}
```

What matters most:

- `network` must be a CAIP-2 XRPL identifier such as `xrpl:1`
- `payTo` is the required destination address
- `asset` and `amount` define the exact acceptable payment
- `maxAmountRequired` matches `amount.value`
- `extra.xrpl.assetId` gives the canonical asset identifier such as `XRP:native` or `RLUSD:rIssuer`

## `PAYMENT-SIGNATURE`

This header is sent by the buyer on the retry request.

Decoded example:

```json
{
  "x402Version": 2,
  "scheme": "exact",
  "network": "xrpl:1",
  "payload": {
    "signedTxBlob": "120000228000000024...",
    "invoiceId": "A7F9C76B2EAC41A9B2D500AA76B8FA18"
  }
}
```

The important fields are:

- `signedTxBlob`: the fully signed XRPL `Payment` transaction blob
- `invoiceId`: optional request correlation value; if present, it must match the XRPL transaction `InvoiceID`

The middleware forwards `signedTxBlob` and `invoiceId` to the facilitator as snake_case JSON fields:

```json
{
  "signed_tx_blob": "120000228000000024...",
  "invoice_id": "A7F9C76B2EAC41A9B2D500AA76B8FA18"
}
```

## `PAYMENT-RESPONSE`

This header is added to the successful seller response after verify and settle pass.

Decoded example:

```json
{
  "x402Version": 2,
  "scheme": "exact",
  "network": "xrpl:1",
  "success": true,
  "payer": "rBuyerAddress...",
  "payTo": "rMerchantAddress...",
  "invoiceId": "A7F9C76B2EAC41A9B2D500AA76B8FA18",
  "txHash": "A1B2C3...",
  "settlementStatus": "validated",
  "asset": {"code": "XRP"},
  "amount": {
    "value": "1000",
    "unit": "drops",
    "drops": 1000
  }
}
```

This is the seller-facing confirmation object. Middleware also injects the same resolved values into `request.state.x402_payment`.

## When To Read Which Shape

- clients should parse `PAYMENT-REQUIRED` first and only fall back to the `402` JSON body if the header is absent or invalid
- middleware and facilitator communicate with snake_case JSON request bodies on `/verify` and `/settle`
- application code usually reads `request.state.x402_payment` instead of parsing `PAYMENT-RESPONSE` directly

For the full lifecycle, continue to [Payment Flow](payment-flow.md). For replay and ledger-window enforcement, continue to [Replay And Freshness](replay-and-freshness.md).

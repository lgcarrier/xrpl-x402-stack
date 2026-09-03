# Canonical HTTP contract

All three x402 headers contain base64-encoded canonical JSON.

## PAYMENT-REQUIRED

```json
{
  "x402Version": 2,
  "resource": {
    "url": "https://merchant.example/premium",
    "description": "Premium data",
    "mimeType": "application/json",
    "serviceName": "Example merchant",
    "tags": ["xrpl", "premium"]
  },
  "accepts": [{
    "scheme": "exact",
    "network": "xrpl:1",
    "asset": "XRP",
    "amount": "1000",
    "payTo": "rMerchant",
    "maxTimeoutSeconds": 60,
    "extra": {
      "areFeesSponsored": false,
      "assetTransferMethod": "sequence"
    }
  }],
  "extensions": {}
}
```

## PAYMENT-SIGNATURE

The payload must repeat the selected accepted requirements exactly:

```json
{
  "x402Version": 2,
  "accepted": {"scheme":"exact","network":"xrpl:1","asset":"XRP","amount":"1000","payTo":"rMerchant","maxTimeoutSeconds":60,"extra":{"areFeesSponsored":false,"assetTransferMethod":"sequence"}},
  "payload": {"signedTxBlob": "120000..."},
  "extensions": {}
}
```

## PAYMENT-RESPONSE

```json
{
  "success": true,
  "payer": "rPayer",
  "transaction": "ABCDEF...",
  "network": "xrpl:1",
  "amount": "1000",
  "extra": {"status": "validated"}
}
```

On uncertainty, `success` is false, `errorReason` is `settlement_pending`, and
`transaction` and `network` remain non-empty. Unknown extensions survive the
challenge/payload lifecycle.

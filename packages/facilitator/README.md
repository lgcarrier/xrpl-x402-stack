# xrpl-x402-facilitator

Non-custodial x402 v2 facilitator for exact XRPL payments.

```bash
export MY_DESTINATION_ADDRESS=rMerchant
export FACILITATOR_BEARER_TOKEN=replace-me
export REDIS_URL=redis://127.0.0.1:6379/0
export NETWORK_ID=xrpl:1
xrpl-x402-facilitator
```

The service exposes `/supported`, `/verify`, `/settle`,
`/discovery/resources`, and `/discovery/search`. Protocol-valid verification or
settlement failures return HTTP 200 with `isValid: false` or `success: false`;
malformed/auth failures use 4xx and infrastructure failures use 5xx.

Verification enforces signature authorization, sequence/ticket state, ledger
expiry, fee cap, destination, asset/issuer/amount, invoice, tag, NetworkID,
forbidden fields, simulation or targeted balance checks, and exact accepted
requirements.

Settlement succeeds only for validated `tesSUCCESS`. Redis atomically reserves
the canonical signed transaction hash and retains uncertain submissions until
ledger expiry. Identical retries reconcile without rebroadcasting.

`SETTLEMENT_MODE` was removed; configuring it fails startup.

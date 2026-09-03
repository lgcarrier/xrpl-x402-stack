# Facilitator package

The facilitator registers `ExactXRPLFacilitatorScheme` with the official
`x402FacilitatorSync` runtime and exposes canonical `/supported`, `/verify`, and
`/settle` envelopes.

## Required configuration

```text
MY_DESTINATION_ADDRESS=rMerchant
FACILITATOR_BEARER_TOKEN=replace-me
REDIS_URL=redis://127.0.0.1:6379/0
NETWORK_ID=xrpl:1
XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
MAX_FEE_DROPS=1000
```

`ALLOWED_ISSUED_ASSETS` is a comma-separated internal configuration list of
`CODE:ISSUER` pairs. It is not an x402 wire representation. RLUSD uses the
current official issuer for the configured network. XRP and RLUSD are built-in
facilitator assets; USDC/custom IOUs require explicit entries.

Do not configure `SETTLEMENT_MODE`; it was removed and now fails startup.

## Verification

The mechanism verifies the exact accepted requirements and signed transaction,
including account master/RegularKey authorization, single-signing, current
sequence or unconsumed ticket, ledger window, fee cap, asset/issuer/amount,
`SendMax`, invoice, tag, `NetworkID`, account state, and simulation/fallback.

It rejects partial payments, paths, `DeliverMin`, delegates, memos,
multisigning, conflicting amount fields, and unexpected transaction fields.

## Settlement

The canonical transaction hash is atomically reserved before submission.
Success requires a validated `tesSUCCESS` with the correct delivered amount.
Timeouts and uncertain RPC errors return `settlement_pending` with a non-empty
hash/network. Identical retries reconcile; they never rebroadcast.

Protocol-valid business failures use HTTP 200. Authentication/malformed input
uses 4xx, and infrastructure failure uses 5xx.

## Discovery

Valid Bazaar extensions are sanitized and indexed in Redis. Use paginated
`/discovery/resources` or deterministic `/discovery/search` queries.

# Replay And Freshness

The facilitator protects exact-pay-per-request flows in two layers:

- Redis replay markers keyed by both `invoice_id` and signed-blob hash
- XRPL ledger-window checks in public-gateway mode

## Replay Keys

Every payment attempt resolves to two replay identifiers:

- `invoice_id`
- `blob_hash`

`invoice_id` comes from the XRPL transaction `InvoiceID` when present. If the buyer omits it entirely, the facilitator falls back to the first 32 hex characters of the signed transaction blob hash.

That means two requests collide if they reuse either:

- the same XRPL `InvoiceID`
- the same signed transaction blob

## Verify vs Settle

`POST /verify` is a read-only guard. It checks whether either replay key already exists and rejects the payment if so.

`POST /settle` is the state-changing step:

1. reserve both replay keys as `pending`
2. submit the transaction to XRPL
3. either convert the reservation to `processed` or release it on failure

This split lets middleware reject obvious replays before trying settlement, while still making settlement itself atomic enough to block double submission races.

## Redis State

Replay markers are stored in Redis as:

- `pending:<reservation_id>` while a settlement is in progress
- `processed` after a successful settlement path

Defaults:

- processed TTL: `REPLAY_PROCESSED_TTL_SECONDS`, default `604800` seconds
- pending TTL: `max(VALIDATION_TIMEOUT + 60, 300)`

If either replay key already exists, the facilitator rejects the payment with:

```text
Transaction already processed (replay attack)
```

## Settlement Mode Effects

### `validated`

In `validated` mode, the facilitator:

1. submits the transaction
2. polls XRPL for up to `VALIDATION_TIMEOUT` seconds
3. waits for `tx.result.validated`
4. checks the delivered amount against the required exact amount
5. returns `status="validated"`

If validation never arrives in time, the pending reservation is released and settlement fails.

### `optimistic`

In `optimistic` mode, the facilitator:

1. submits the transaction
2. marks the replay reservation processed immediately
3. returns `status="submitted"`

This mode lowers latency, but it shifts more validation responsibility to the surrounding system.

## Freshness Rules In `redis_gateways` Mode

When `GATEWAY_AUTH_MODE=redis_gateways`, the facilitator additionally requires bounded XRPL timing:

- the signed transaction must include `LastLedgerSequence`
- `LastLedgerSequence` must be greater than the latest validated ledger
- `LastLedgerSequence` must not exceed `current_validated_ledger + MAX_PAYMENT_LEDGER_WINDOW`

If any of those checks fail, the facilitator rejects the payment before settlement.

This is why public-gateway mode is stricter than `single_token`: it assumes third-party sellers may retry or relay traffic, so the facilitator enforces a narrow ledger window for safer exact-payment handling.

## Practical Guidance

- generate a fresh signed transaction per paid request
- use `invoice_id_factory` on the client when you want stable cross-service correlation
- leave XRPL autofill enabled, or set `LastLedgerSequence` yourself, when using `redis_gateways`
- prefer `validated` settlement for internet-facing deployments

For the on-the-wire request/response format, continue to [Header Contract](header-contract.md).

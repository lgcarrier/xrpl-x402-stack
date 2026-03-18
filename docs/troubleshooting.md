# Troubleshooting

## `XRPL_WALLET_SEED is required`

Run `python -m devtools.quickstart` first, or make sure `.env.quickstart` includes `XRPL_WALLET_SEED`.

## Quickstart cannot find a healthy public XRPL Testnet RPC endpoint

The quickstart and top-up helpers probe a small list of public Testnet JSON-RPC servers by default.
If all of them are unavailable from your machine, pin one explicitly:

```bash
export XRPL_TESTNET_RPC_URL=https://your-testnet-rpc.example/
python -m devtools.quickstart
```

You can also pass `--xrpl-rpc-url ...` to `devtools.quickstart`, `devtools.rlusd_topup`, or
`devtools.usdc_topup`.

For the generated runtime stack, keep using `XRPL_RPC_URL` in `.env.quickstart` or your shell if
you want to pin the buyer, payer, or facilitator to a specific RPC provider.

## Docker Compose starts, but the buyer still gets `402`

- confirm the facilitator and merchant are using the same `FACILITATOR_BEARER_TOKEN`
- make sure the buyer is using the same `XRPL_NETWORK` as the merchant route
- for issued assets, confirm `PAYMENT_ASSET` matches the merchant `PRICE_ASSET_CODE` and `PRICE_ASSET_ISSUER`
- inspect the decoded `PAYMENT-REQUIRED` challenge and the retry shape described in [Header Contract](how-it-works/header-contract.md)
- confirm the facilitator reports the expected asset and settlement mode from `GET /supported`

## `Provided invoice_id does not match transaction InvoiceID`

The buyer sent an `invoice_id` in the x402 payload that does not match the XRPL transaction `InvoiceID`.

Fix one of these:

- generate both values from the same source, for example with `invoice_id_factory` on the client
- stop sending a payload `invoice_id` if you do not need explicit request correlation
- inspect the signed transaction you are generating and confirm it actually contains the expected `InvoiceID`

If you omit `invoice_id` entirely, the facilitator falls back to a hash-derived value.

## The facilitator cannot start

- confirm Redis is reachable at `redis://redis:6379/0` inside Docker Compose
- confirm `MY_DESTINATION_ADDRESS` and `FACILITATOR_BEARER_TOKEN` are set
- if you are using `redis_gateways`, confirm your bearer token exists in Redis with `status=active` and a non-empty `gateway_id`

## `Transaction already processed (replay attack)`

The facilitator saw the same `invoice_id` or signed transaction blob more than once.

Check [Replay And Freshness](how-it-works/replay-and-freshness.md) if you need the exact Redis behavior. In practice:

- do not reuse the same signed transaction blob for multiple paid requests
- generate a fresh `invoice_id` per request when you want explicit correlation
- if a prior settlement failed before returning to the buyer, inspect facilitator logs before retrying blindly

## `Transaction LastLedgerSequence required in redis_gateways mode`

Public-gateway mode requires every payment to carry a bounded `LastLedgerSequence`.

Fix one of these:

- enable XRPL autofill on the buyer signer so the transaction gets a ledger bound automatically
- set `LastLedgerSequence` yourself before signing
- switch back to `single_token` mode for local-only demos

The full rule set is documented in [Replay And Freshness](how-it-works/replay-and-freshness.md).

## RLUSD claims are rate limited

Rerun `python -m devtools.rlusd_topup` later. The helper records local cooldown state under `.live-test-wallets/rlusd-claim-state.json`.

## USDC does not appear after the Circle faucet claim

Rerun `python -m devtools.usdc_topup` after the faucet transfer is visible on XRPL Testnet. The helper is designed to recover and sweep later claims.

# Troubleshooting

## `XRPL_WALLET_SEED is required`

Run `python -m devtools.quickstart` first, or make sure `.env.quickstart` includes `XRPL_WALLET_SEED`.

## Docker Compose starts, but the buyer still gets `402`

- confirm the facilitator and merchant are using the same `FACILITATOR_BEARER_TOKEN`
- make sure the buyer is using the same `XRPL_NETWORK` as the merchant route
- for issued assets, confirm `PAYMENT_ASSET` matches the merchant `PRICE_ASSET_CODE` and `PRICE_ASSET_ISSUER`

## The facilitator cannot start

- confirm Redis is reachable at `redis://redis:6379/0` inside Docker Compose
- confirm `MY_DESTINATION_ADDRESS` and `FACILITATOR_BEARER_TOKEN` are set

## RLUSD claims are rate limited

Rerun `python -m devtools.rlusd_topup` later. The helper records local cooldown state under `.live-test-wallets/rlusd-claim-state.json`.

## USDC does not appear after the Circle faucet claim

Rerun `python -m devtools.usdc_topup` after the faucet transfer is visible on XRPL Testnet. The helper is designed to recover and sweep later claims.

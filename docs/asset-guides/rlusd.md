# RLUSD Guide

Use this after the [Testnet XRP quickstart](../quickstart/testnet-xrp.md) is already working.

## What This Guide Covers

- creating or recovering the disposable RLUSD claim wallet
- claiming RLUSD with the existing helper flow
- switching the merchant and buyer demo from XRP to RLUSD

## Prerequisites

- the XRP quickstart has completed successfully
- you have a `TRYRLUSD_SESSION_TOKEN` for the RLUSD faucet flow

Export the session token:

```bash
export TRYRLUSD_SESSION_TOKEN=...
```

Then run the helper:

```bash
python -m devtools.rlusd_topup
```

The helper will:

- reuse the cached Testnet wallet pair
- create or recover a disposable RLUSD claim wallet
- create the RLUSD trustline
- attempt the faucet claim when the session and cooldown allow it
- sweep claimed RLUSD back into the canonical wallet when possible

If the helper reports a pending or rate-limited claim, rerun it later.

## Switch The Demo To RLUSD

Edit `.env.quickstart` and set:

```dotenv
PRICE_ASSET_CODE=RLUSD
PRICE_ASSET_ISSUER=rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV
PRICE_ASSET_AMOUNT=1.25
PAYMENT_ASSET=RLUSD:rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart up --build
docker compose --env-file .env.quickstart --profile demo run --rm buyer
```

The merchant example will price `/premium` in RLUSD, and the buyer example will select the matching issued-asset payment option.

## Notes

- The default Testnet RLUSD issuer can be overridden with `XRPL_TESTNET_RLUSD_ISSUER`.
- Claim state is stored under `.live-test-wallets/rlusd-claim-state.json`.
- The helper manages trustline cleanup and account deletion for disposable claim wallets when the ledger allows it.

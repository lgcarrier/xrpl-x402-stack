# USDC Guide

Use this after the [Testnet XRP quickstart](../quickstart/testnet-xrp.md) is already working.

## What This Guide Covers

- preparing the disposable USDC claim wallet
- manually claiming USDC from the Circle faucet
- sweeping USDC back into the canonical wallet
- switching the merchant and buyer demo from XRP to USDC

## Prepare The Claim Wallet

Run:

```bash
python -m devtools.usdc_topup
```

The helper prints a disposable wallet address and tells you when a manual Circle faucet claim is required.

Follow the printed instruction:

1. visit <https://faucet.circle.com/>
2. choose XRPL Testnet
3. claim USDC to the disposable wallet address
4. rerun `python -m devtools.usdc_topup`

On the rerun, the helper sweeps the claimed USDC into the canonical wallet when the funds are visible on-ledger.
It then funds the dedicated USDC buyer wallet so the USDC demo signs with the
same wallet that received the top-up.

## Switch The Demo To USDC

Generate a derived env file:

```bash
python -m devtools.demo_env --asset usdc
```

That writes `.env.quickstart.usdc` with the USDC merchant pricing, buyer asset
selection, and the dedicated USDC buyer seed.

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart.usdc up --build
docker compose --env-file .env.quickstart.usdc --profile demo run --rm buyer
```

## Notes

- The default Testnet USDC issuer can be overridden with `XRPL_TESTNET_USDC_ISSUER`.
- Claim state is stored under `.live-test-wallets/usdc-claim-state.json`.
- The helper handles trustline setup, sweeping, cleanup, and delete retries around the manual faucet flow.
- The USDC buyer wallet is separate from the XRP and RLUSD buyers so those demo runs can sign in parallel.

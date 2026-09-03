# RLUSD Guide

Use the browser-free funding command to create an XRPL Testnet wallet with XRP
and official Testnet RLUSD. It does not connect a wallet in a browser, retain a
GitHub session, or require Playwright.

## Create And Fund A New Wallet

From the repository root with the virtual environment active:

```bash
python -m devtools.rlusd_fund \
  --new-wallet \
  --target-rlusd 10 \
  --max-xrp 35
```

The command persists the seed before requesting XRP, creates the official RLUSD
trust line, quotes an XRP-to-RLUSD route, and submits an exact-output circular
Payment. `--max-xrp` is the absolute XRP `SendMax` for the conversion, not a
request for that exact faucet balance.

The output includes the public address, validated XRP/RLUSD balances,
transaction hashes, and a private wallet-file path. It never prints the seed.
The wallet and recovery state are stored under the Git-ignored
`.live-test-wallets/` directory with private file permissions.

## Resume Safely

If the command reports `pending` (exit status `3`), rerun it with the exact
wallet path it printed:

```bash
python -m devtools.rlusd_fund \
  --wallet-file .live-test-wallets/rlusd-funded-wallet-YYYYMMDDTHHMMSSZ.json \
  --target-rlusd 10 \
  --max-xrp 35
```

The retry checks the journaled transaction hash first and only rebroadcasts the
same signed blob while it remains live. It does not sign another conversion
until the previous transaction validates or is authoritatively absent after
expiry. Target-balance semantics also make a completed rerun a no-op.

## Fund The Cached Demo Buyer

After the [Testnet XRP quickstart](../quickstart/testnet-xrp.md), omit both
wallet-selection options to fund the dedicated cached RLUSD buyer:

```bash
python -m devtools.rlusd_fund --target-rlusd 10 --max-xrp 35
```

The command is Testnet-only, uses the repository's official Testnet issuer, caps
every transaction fee, rejects partial payments, withholds success until a
validated `tesSUCCESS`, and checks the final RLUSD balance. Exit status `2`
means current liquidity or the configured XRP cap prevented funding; exit
status `1` means validation or infrastructure failed. If this cached-wallet
command exits with status `3`, rerun the same command without `--wallet-file`;
the multi-wallet cache is not a standalone wallet file.

## Switch The Demo To RLUSD

Generate a derived env file:

```bash
python -m devtools.demo_env --asset rlusd
```

That writes `.env.quickstart.rlusd` with the RLUSD merchant pricing, buyer asset selection,
the RLUSD buyer seed, and any facilitator-side `ALLOWED_ISSUED_ASSETS` entry
needed for the chosen issuer.

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart.rlusd up --build
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

The merchant example will price `/premium` in RLUSD, and the buyer example will select the matching issued-asset payment option.

## Notes

- The funding command intentionally refuses non-official RLUSD issuers.
- Preserve the wallet file if an operation is pending; it is required for safe reconciliation.
- The legacy session-token top-up helper remains available but is no longer the recommended workflow.
- The RLUSD buyer wallet is separate from the XRP and USDC buyers so those demo runs can sign in parallel.

# Guided Quickstart: Testnet XRP

This is the fastest real end-to-end path:

1. generate or reuse XRPL Testnet wallets
2. write a local `.env.quickstart` file
3. run the facilitator and merchant with Docker Compose
4. have the buyer example pay for a protected route with XRP

## Prerequisites

- Python `3.12`
- Docker Desktop or Docker Engine with Compose
- outbound network access to XRPL Testnet

## Setup

Create a virtualenv and install the repo dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Generate the quickstart env file:

```bash
python -m devtools.quickstart
```

That command:

- creates or reuses a cached XRPL Testnet wallet pair
- writes `.env.quickstart`
- prints the merchant address, buyer address, buyer seed, and the exact next commands

The generated file contains secrets and is ignored by git. Do not commit it.

## Run The Stack

Start the facilitator, merchant, and Redis:

```bash
docker compose --env-file .env.quickstart up --build
```

In a second terminal, trigger the paid request:

```bash
docker compose --env-file .env.quickstart run --rm --profile demo buyer
```

Expected output:

```text
status=200
{"message":"premium content unlocked", ...}
```

## What Happened

- the merchant challenged the first request with `402 Payment Required`
- the buyer decoded `PAYMENT-REQUIRED`, signed an exact XRP payment, and retried once
- the facilitator verified and settled the presigned transaction
- the middleware injected `request.state.x402_payment` and returned the protected content

## Useful Files

- `.env.quickstart`
- `.live-test-wallets/xrpl-testnet-wallets.json`
- `examples/merchant_fastapi/app.py`
- `examples/buyer_httpx.py`

## Clean Up

Stop the stack with `Ctrl+C`, or run:

```bash
docker compose --env-file .env.quickstart down
```

You can reuse `.env.quickstart` and the cached wallets the next time you run the demo.

## Next Steps

- Follow the [RLUSD guide](../asset-guides/rlusd.md)
- Follow the [USDC guide](../asset-guides/usdc.md)
- Read [Payment Flow](../how-it-works/payment-flow.md)

# Open XRPL x402 Stack

Hosted docs for the XRPL-first x402 stack:

- `xrpl-x402-core`
- `xrpl-x402-facilitator`
- `xrpl-x402-middleware`
- `xrpl-x402-client`

## Start Here

If you want to see a real payment succeed on XRPL Testnet, go straight to the [Testnet XRP quickstart](quickstart/testnet-xrp.md).

That flow uses:

- `python -m devtools.quickstart` to generate reusable Testnet wallets and `.env.quickstart`
- `docker compose --env-file .env.quickstart up --build` to run the facilitator and merchant
- `docker compose --env-file .env.quickstart run --rm --profile demo buyer` to trigger the paid request

## Package Chooser

- Use [Facilitator](packages/facilitator.md) when you need the verifier/settler service.
- Use [Middleware](packages/middleware.md) when you want to protect ASGI or FastAPI routes.
- Use [Client](packages/client.md) when you want a buyer-side SDK that signs XRPL payments and retries `402` responses.
- Use [Core](packages/core.md) when you need the shared wire models, codecs, and helpers directly.

## Beyond XRP

The primary quickstart is XRP on Testnet for the fastest real success path.

After that:

- use the [RLUSD guide](asset-guides/rlusd.md) to claim and sweep RLUSD
- use the [USDC guide](asset-guides/usdc.md) to prepare a Circle faucet wallet and sweep USDC

## What The Stack Does

The stack keeps the facilitator contract stable:

- `GET /health`
- `GET /supported`
- `POST /verify`
- `POST /settle`

The request flow is documented in [Payment Flow](how-it-works/payment-flow.md).

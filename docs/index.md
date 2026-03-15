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
- Use [Middleware](packages/middleware.md) when you want to protect ASGI or FastAPI routes. Start with the package quickstart on that page.
- Use [Client](packages/client.md) when you want a buyer-side SDK that signs XRPL payments and retries `402` responses. Start with the package quickstart on that page.
- Use [Core](packages/core.md) when you need the shared wire models, codecs, and helpers directly.

## Beyond XRP

The primary quickstart is XRP on Testnet for the fastest real success path.

When you switch the demo to issued assets, the merchant uses `PRICE_*` variables and the buyer uses `PAYMENT_ASSET`.

### RLUSD Demo Config

Set these values in `.env.quickstart`:

```dotenv
PRICE_ASSET_CODE=RLUSD
PRICE_ASSET_ISSUER=rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV
PRICE_ASSET_AMOUNT=1.25
PAYMENT_ASSET=RLUSD:rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart up --build
docker compose --env-file .env.quickstart run --rm --profile demo buyer
```

Use the [RLUSD guide](asset-guides/rlusd.md) for faucet setup, trustline details, and sweep behavior.

### USDC Demo Config

Set these values in `.env.quickstart`:

```dotenv
PRICE_ASSET_CODE=USDC
PRICE_ASSET_ISSUER=rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt
PRICE_ASSET_AMOUNT=2.50
PAYMENT_ASSET=USDC:rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart up --build
docker compose --env-file .env.quickstart run --rm --profile demo buyer
```

Use the [USDC guide](asset-guides/usdc.md) for the Circle faucet flow and sweep behavior.

## What The Stack Does

The stack keeps the facilitator contract stable:

- `GET /health`
- `GET /supported`
- `POST /verify`
- `POST /settle`

The request flow is documented in [Payment Flow](how-it-works/payment-flow.md).
The exact header shapes are documented in [Header Contract](how-it-works/header-contract.md).
Replay protection and public-gateway freshness rules are documented in [Replay And Freshness](how-it-works/replay-and-freshness.md).
Optional Coinbase Python `x402` interop is documented in [Coinbase x402 Adapters](integrations/x402-adapters.md).

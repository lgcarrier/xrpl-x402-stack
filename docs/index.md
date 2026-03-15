# Open XRPL x402 Stack

Hosted docs for the XRPL-first x402 stack:

- `xrpl-x402-core`
- `xrpl-x402-facilitator`
- `xrpl-x402-middleware`
- `xrpl-x402-client`

[![GitHub repository](https://img.shields.io/badge/GitHub-xrpl--x402--stack-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack)
[![Docs version](https://img.shields.io/badge/docs-v0.1.0-0A7E3B)](release.md)

## Start Here

If you want to see a real payment succeed on XRPL Testnet, go straight to the [Testnet XRP quickstart](quickstart/testnet-xrp.md).

That flow uses:

- `python -m devtools.quickstart` to generate reusable Testnet wallets and `.env.quickstart`
- `docker compose --env-file .env.quickstart up --build` to run the facilitator and merchant
- `docker compose --env-file .env.quickstart run --rm --profile demo buyer` to trigger the paid request

## Package Chooser

| Package | PyPI | Install | Use when |
| --- | --- | --- | --- |
| [Core](packages/core.md) | [![PyPI package](https://img.shields.io/badge/PyPI-xrpl--x402--core-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-core/) | `pip install xrpl-x402-core` | You need the shared XRPL/x402 models, codecs, and helpers directly. |
| [Facilitator](packages/facilitator.md) | [![PyPI package](https://img.shields.io/badge/PyPI-xrpl--x402--facilitator-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-facilitator/) | `pip install xrpl-x402-facilitator` | You need the verifier/settler service. |
| [Middleware](packages/middleware.md) | [![PyPI package](https://img.shields.io/badge/PyPI-xrpl--x402--middleware-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-middleware/) | `pip install xrpl-x402-middleware` | You want to protect ASGI or FastAPI routes. |
| [Client](packages/client.md) | [![PyPI package](https://img.shields.io/badge/PyPI-xrpl--x402--client-3775A9?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-client/) | `pip install xrpl-x402-client` | You want a buyer-side SDK that signs XRPL payments and retries `402` responses. |

## Install Commands

```bash
pip install xrpl-x402-core
pip install xrpl-x402-facilitator
pip install xrpl-x402-middleware
pip install xrpl-x402-client
```

Optional Coinbase Python `x402` interop:

```bash
pip install "xrpl-x402-middleware[x402]"
pip install "xrpl-x402-client[x402]"
```

## Comparison Table

| Package | Runs where | Main entry points | Depends on facilitator | Optional extras |
| --- | --- | --- | --- | --- |
| `xrpl-x402-core` | Shared library code | `PaymentRequired`, `PaymentPayload`, `PaymentResponse`, header codecs | No | None |
| `xrpl-x402-facilitator` | Seller infrastructure / service tier | `create_app(...)`, `xrpl_x402_facilitator.main:app`, `xrpl-x402-facilitator` | It is the facilitator | None |
| `xrpl-x402-middleware` | Seller app | `PaymentMiddlewareASGI`, `require_payment(...)`, `XRPLFacilitatorClient` | Yes | `[x402]` |
| `xrpl-x402-client` | Buyer app or integration test harness | `XRPLPaymentSigner`, `XRPLPaymentTransport`, `wrap_httpx_with_xrpl_payment(...)` | Yes, against a protected seller route | `[x402]` |

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

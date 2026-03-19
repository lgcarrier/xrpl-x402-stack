# Open XRPL x402 Stack

Hosted docs for the XRPL-first x402 stack:

- `xrpl-x402-core`
- `xrpl-x402-facilitator`
- `xrpl-x402-middleware`
- `xrpl-x402-client`
- `xrpl-x402-payer`

[![GitHub repository](https://img.shields.io/badge/GitHub-xrpl--x402--stack-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack)
[![Docs version](https://img.shields.io/badge/docs-v0.1.0-0A7E3B)](release.md)

## Start Here

If you want to see a real payment succeed on XRPL Testnet, go straight to the [Testnet XRP quickstart](quickstart/testnet-xrp.md).

That flow uses:

- `python -m devtools.quickstart` to generate reusable Testnet wallets and `.env.quickstart`
- `docker compose --env-file .env.quickstart up --build` to run the facilitator and merchant
- `docker compose --env-file .env.quickstart --profile demo run --rm buyer` to trigger the paid request

The quickstart probes public XRPL Testnet RPC servers and writes the first healthy endpoint into
`.env.quickstart` as `XRPL_RPC_URL`. Override the devtools-side selection with
`XRPL_TESTNET_RPC_URL` or `python -m devtools.quickstart --xrpl-rpc-url ...`.

## Package Chooser

Pick the package for the role you are building. Most integrators start with `xrpl-x402-middleware` on the seller side or `xrpl-x402-client` on the buyer side, then add `xrpl-x402-facilitator` as the verifier/settler service.

| Package | PyPI | Install | Use when |
| --- | --- | --- | --- |
| [Core](packages/core.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-core?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-core/) | `pip install xrpl-x402-core` | You need the shared XRPL/x402 models, codecs, and header helpers directly. |
| [Facilitator](packages/facilitator.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-facilitator?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-facilitator/) | `pip install xrpl-x402-facilitator` | You are running the verifier/settler service that sellers call during verify and settle. |
| [Middleware](packages/middleware.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-middleware?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-middleware/) | `pip install xrpl-x402-middleware` | You are protecting ASGI or FastAPI routes that should return `402` until paid. |
| [Client](packages/client.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-client?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-client/) | `pip install xrpl-x402-client` | You are building a buyer that signs XRPL payments and retries `402` challenges automatically. |
| [Payer](packages/payer.md) | [![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-payer?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-payer/) | `pip install xrpl-x402-payer` | You want a turnkey buyer CLI, local proxy, receipts, or native MCP support for Claude Desktop and Cursor. |

If you want the shortest path to a working stack, read the [middleware quickstart](packages/middleware.md), the [client quickstart](packages/client.md), then run the [Testnet XRP quickstart](quickstart/testnet-xrp.md).

## Install Commands

```bash
pip install xrpl-x402-core
pip install xrpl-x402-facilitator
pip install xrpl-x402-middleware
pip install xrpl-x402-client
pip install xrpl-x402-payer
```

Optional Coinbase Python `x402` interop:

```bash
pip install "xrpl-x402-middleware[x402]"
pip install "xrpl-x402-client[x402]"
```

Full AI agent support:

```bash
pip install "xrpl-x402-payer[mcp]"
xrpl-x402 skill install
xrpl-x402 mcp
claude mcp add xrpl-x402-payer -- xrpl-x402 mcp
```

## Comparison Table

| Package | Runs where | Main entry points | Depends on facilitator | Optional extras |
| --- | --- | --- | --- | --- |
| `xrpl-x402-core` | Shared library code | `PaymentRequired`, `PaymentPayload`, `PaymentResponse`, header codecs | No | None |
| `xrpl-x402-facilitator` | Seller infrastructure / service tier | `create_app(...)`, `xrpl_x402_facilitator.main:app`, `xrpl-x402-facilitator` | It is the facilitator | None |
| `xrpl-x402-middleware` | Seller app | `PaymentMiddlewareASGI`, `require_payment(...)`, `XRPLFacilitatorClient` | Yes | `[x402]` |
| `xrpl-x402-client` | Buyer app or integration test harness | `XRPLPaymentSigner`, `XRPLPaymentTransport`, `wrap_httpx_with_xrpl_payment(...)` | Yes, against a protected seller route | `[x402]` |
| `xrpl-x402-payer` | Buyer operator / local agent runtime | `xrpl-x402`, `pay_with_x402(...)`, `XRPLPayer`, bundled skill, stdio MCP server | Yes, against a protected seller route | `[mcp]` |

## Beyond XRP

The primary quickstart is XRP on Testnet for the fastest real success path.

When you switch the demo to issued assets, the merchant uses `PRICE_*` variables and the buyer uses `PAYMENT_ASSET`.

### RLUSD Demo Config

Generate a derived env file:

```bash
python -m devtools.demo_env --asset rlusd
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart.rlusd up --build
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

Use the [RLUSD guide](asset-guides/rlusd.md) for faucet setup, trustline details, and sweep behavior.

### USDC Demo Config

Generate a derived env file:

```bash
python -m devtools.demo_env --asset usdc
```

Then restart the stack and rerun the buyer:

```bash
docker compose --env-file .env.quickstart.usdc up --build
docker compose --env-file .env.quickstart.usdc --profile demo run --rm buyer
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

# Open XRPL x402 Stack

MIT-licensed, Python-first, exact-pay-per-request XRPL infrastructure for the open `x402` protocol.

This repo now ships four packages from one monorepo:

| Package | Purpose |
| --- | --- |
| `xrpl-x402-core` | Shared XRPL/x402 wire models, codecs, validation, and exact-payment helpers |
| `xrpl-x402-facilitator` | FastAPI facilitator service, app factory, CLI, and Docker image |
| `xrpl-x402-middleware` | Seller-side ASGI/FastAPI route protection with `PAYMENT-*` headers |
| `xrpl-x402-client` | Buyer-side payment signing helpers and `httpx` retry support |

The stack is independently implemented for the open `x402` protocol and does not copy `x402-xrpl`.

## Package Chooser

- Use `xrpl-x402-facilitator` when you need a self-hosted verifier/settler for presigned XRPL payments.
- Use `xrpl-x402-middleware` when you want to protect FastAPI or ASGI routes with exact XRPL payments.
- Use `xrpl-x402-client` when you want a buyer-side Python SDK that can decode `402` challenges, sign XRPL payments, and retry via `httpx`.
- Use `xrpl-x402-core` when you need the shared models and helper functions directly.

## Repo Layout

```text
packages/
  core/
  facilitator/
  middleware/
  client/
examples/
  merchant_fastapi/
  buyer_httpx.py
tests/
```

## Local Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Common commands:

```bash
xrpl-x402-facilitator --reload
uvicorn examples.merchant_fastapi.app:app --reload --port 8010
pytest
PYTHONPYCACHEPREFIX=/tmp/pycache python -m compileall packages tests examples devtools
```

## Facilitator Contract

The facilitator HTTP API stays stable:

- `GET /health`
- `GET /supported`
- `POST /verify`
- `POST /settle`

## x402 Compatibility

Optional adapters are available for the upstream Python `x402` SDK:

- `xrpl_x402_client.adapters.x402`
- `xrpl_x402_middleware.adapters.x402`

They are kept behind optional dependencies so the stack remains usable without installing `x402`.

## Examples

Merchant example:

```bash
uvicorn examples.merchant_fastapi.app:app --reload --port 8010
```

Buyer example:

```bash
export XRPL_WALLET_SEED=...
python examples/buyer_httpx.py
```

Docker Compose demo:

```bash
docker compose up --build
docker compose run --rm --profile demo buyer
```

The buyer demo expects a funded XRPL wallet seed in `XRPL_WALLET_SEED`.

## Release Model

Packages version independently and publish from tag prefixes:

- `core-v*`
- `facilitator-v*`
- `middleware-v*`
- `client-v*`

## Verification

Default suite:

```bash
pytest
```

Settlement-path verification:

```bash
pytest
RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s
docker build -t xrpl-x402-facilitator .
```

# Open XRPL x402 Stack

Python packages for canonical x402 v2 exact payments on the XRP Ledger. Release
0.2.0 pins `x402==2.21.0` and implements the XRPL client, resource-server, and
facilitator mechanisms where the upstream Python SDK has no XRPL mechanism.

The stack is v2-only. It does not accept the 0.1 hybrid bodies, legacy field
aliases, or optimistic settlement responses.

## Packages

| Package | Purpose |
| --- | --- |
| `xrpl-x402-core` | Official schema re-exports and XRPL-specific helpers |
| `xrpl-x402-client` | Exact XRPL transaction construction and x402 client registration |
| `xrpl-x402-middleware` | HTTP/MCP resource-server integration and response withholding |
| `xrpl-x402-facilitator` | Canonical facilitator API, verification, settlement, replay, discovery |
| `xrpl-x402-payer` | HTTP/CLI/MCP payer with spend controls and standard receipts |

## Install for development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

The exact runtime dependency is `x402[extensions]==2.21.0`. The middleware and
payer MCP extras also pin `x402[mcp]==2.21.0` and MCP 1.x.

## Run the local stack

```bash
cp .env.example .env
# Set MY_DESTINATION_ADDRESS and FACILITATOR_BEARER_TOKEN.
docker compose up --build
```

The facilitator serves canonical `/supported`, `/verify`, and `/settle`
endpoints. The example merchant protects `GET /premium` with
`PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and `PAYMENT-RESPONSE` headers.

## Canonical XRPL representation

- XRP uses `asset: "XRP"` and an integer drops string in `amount`.
- IOUs use the XRPL currency code in `asset` and `extra.issuer`.
- `extra.areFeesSponsored` is always false.
- `assetTransferMethod` is `sequence` by default; `ticketSequence` is opt-in.
- `invoiceId` is advertised as text and committed to XRPL as its SHA-256
  `InvoiceID`; `destinationTag` is copied exactly.

Only official x402 models own protocol messages. Local models are limited to
XRPL payload/extra and durable settlement state.

## Safety model

Authorization flow is verify, execute handler, settle, then release response.
Protected bytes are buffered until a validated `tesSUCCESS`. Indeterminate
settlement returns `settlement_pending` with a transaction hash; identical
retries reconcile the reserved hash and never rebroadcast it. Redis coordinates
transaction reservations and payment-identifier idempotency across instances.

Default automatic payment selection accepts recognized pegged assets with a USD
1 cap. XRP, USDC, and custom IOUs require an explicit asset, issuer where
applicable, and per-payment cap.

## Fund An RLUSD Testnet Wallet

Create a fresh wallet, request XRP from the Testnet faucet, and acquire exactly
enough official Testnet RLUSD to reach a target balance without browser wallet
connections:

```bash
python -m devtools.rlusd_fund \
  --new-wallet \
  --target-rlusd 10 \
  --max-xrp 35
```

The command prints the private wallet-file path but never its seed. Reuse that
path with `--wallet-file` if an XRP faucet request or ledger transaction is
pending. See the [RLUSD guide](docs/asset-guides/rlusd.md) for recovery details.

## Documentation

- [0.1 to 0.2 migration](docs/migration-0.2.md)
- [Protocol and header contract](docs/how-it-works/header-contract.md)
- [Facilitator deployment](docs/packages/facilitator.md)
- [Middleware and MCP](docs/packages/middleware.md)
- [Testing](docs/release.md)

Live Testnet tests are opt-in and never run against Mainnet by default.

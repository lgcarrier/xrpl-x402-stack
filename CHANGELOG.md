# Changelog

## 0.2.0

- Pins the official Python protocol runtime to `x402==2.21.0` and makes all wire
  traffic canonical v2 only.
- Adopts x402 2.21 payment-flow handlers for XRPL authorization and its
  exactly-one automatic retry of an identical `settlement_pending` envelope.
  A later identical request resumes any still-pending settlement from the
  cached protected response without repeating the handler.
- Adds exact XRPL client, resource-server, and facilitator mechanisms for
  `xrpl:0`, `xrpl:1`, and `xrpl:2`, including sequence and ticket authorization.
- Enforces master/RegularKey signatures, NetworkID, invoice hashing, destination
  tags, ledger expiry, fee caps, IOU rules, forbidden fields, and simulation or
  targeted account checks.
- Replaces optimistic settlement with validated `tesSUCCESS`, durable Redis
  transaction reservations, `settlement_pending` reconciliation, and scoped
  payment-identifier idempotency.
- Moves HTTP and MCP integrations to upstream wrappers, adds Bazaar discovery,
  response withholding/recovery, issuer-aware spend controls, and standard
  receipts.
- Adds `python -m devtools.rlusd_fund`, a browser-free XRPL Testnet workflow
  that creates or reuses a private wallet, funds XRP, establishes the official
  RLUSD trust line, and acquires an exact target balance with spend, fee,
  finality, and crash-safe reconciliation controls.
- Adds canonical header fixtures, official TypeScript XRPL cross-SDK vectors,
  real Redis concurrency tests, and completed opt-in Testnet acceptance for XRP
  and official RLUSD with both sequence and ticket authorization.

See [the migration guide](docs/migration-0.2.md) for the intentional 0.1 break.

All notable changes to the Open XRPL x402 Stack are documented here.

## xrpl-x402-facilitator 0.1.1

- Updated published dependency metadata to use `xrpl-py==4.5.0`, allow `uvicorn>=0.30.6,<1`, and allow `python-dotenv>=1.0.1,<2` so facilitator installs can coexist with the payer MCP extra.

## xrpl-x402-middleware 0.1.1

- Constrained the Starlette dependency to `>=0.37,<0.39` to stay aligned with the FastAPI version used by the facilitator package.

## xrpl-x402-payer 0.1.3

- Constrained the Starlette dependency to `>=0.37,<0.39` so `xrpl-x402-payer[mcp]` can be installed alongside the facilitator without upgrading FastAPI's Starlette dependency out of range.

## xrpl-x402-core 0.1.2

- Added `xrpl_currency_code(...)` as the shared helper for rendering XRPL issued-currency codes from 3-character, 20-byte ASCII, or 40-character hex asset identifiers.
- Relaxed issued-amount equality checks in exact-payment matching so numerically equivalent decimal strings compare correctly.

## xrpl-x402-client 0.1.2

- Normalized issued-currency codes before building signed XRPL payments, so non-3-character asset codes serialize in XRPL wire format correctly.
- Accepted lowercase `payment-required` response headers in addition to the canonical casing when decoding x402 challenges.
- Raised the `xrpl-x402-core` dependency floor to `0.1.2` to require the shared issued-currency encoding helper.

## xrpl-x402-core 0.1.1

- Added the shared XRPL Testnet RPC resolver used by quickstart tooling and other Testnet-aware flows to find a healthy public JSON-RPC endpoint.

## xrpl-x402-payer 0.1.2

- Added automatic public XRPL Testnet RPC selection when `XRPL_RPC_URL` is unset and `XRPL_NETWORK=xrpl:1`.
- Raised the `xrpl-x402-core` dependency floor to `0.1.1` so clean installs include the shared Testnet RPC resolver.

## xrpl-x402-core 0.1.0

- Initial public release of the shared XRPL/x402 wire models, header codecs, asset helpers, and exact-payment matching utilities.
- Publishes the canonical `PaymentRequired`, `PaymentPayload`, and `PaymentResponse` models used across the stack.

## xrpl-x402-facilitator 0.1.0

- Initial public release of the FastAPI facilitator service with stable `GET /health`, `GET /supported`, `POST /verify`, and `POST /settle` endpoints.
- Publishes the `create_app(...)` app factory, `xrpl_x402_facilitator.main:app`, and `xrpl-x402-facilitator` CLI.

## xrpl-x402-middleware 0.1.0

- Initial public release of the seller-side ASGI middleware for exact XRPL x402 payments.
- Publishes `PaymentMiddlewareASGI`, `require_payment(...)`, `XRPLFacilitatorClient`, and optional `x402` adapter helpers.

## xrpl-x402-client 0.1.0

- Initial public release of the buyer-side SDK for decoding `402` challenges, signing XRPL payments, and retrying requests via `httpx`.
- Publishes optional `x402` adapter helpers for XRPL exact-payment interop.

## xrpl-x402-payer 0.1.0

- Added official MCP server + CLI integration for buyer-side XRPL x402 payments.
- Publishes the `xrpl-x402` CLI, bundled payer skill, local auto-pay proxy, receipt tracking, and stdio MCP tools for Claude Desktop and Cursor.

## xrpl-x402-client 0.1.1

- Updated `xrpl-py` compatibility to `4.5.0` so downstream payer/MCP installs resolve cleanly.

## xrpl-x402-payer 0.1.1

- Updated the client dependency floor to `xrpl-x402-client>=0.1.1` so clean installs resolve the MCP-compatible XRPL dependency set.

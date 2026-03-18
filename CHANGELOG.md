# Changelog

All notable changes to the Open XRPL x402 Stack are documented here.

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

## xrpl-x402-client 0.1.1

- Updated `xrpl-py` compatibility to `4.5.0` so downstream payer/MCP installs resolve cleanly.

## xrpl-x402-payer 0.1.0

- Added official MCP server + CLI integration for buyer-side XRPL x402 payments.
- Publishes the `xrpl-x402` CLI, bundled payer skill, local auto-pay proxy, receipt tracking, and stdio MCP tools for Claude Desktop and Cursor.

## xrpl-x402-payer 0.1.1

- Updated the client dependency floor to `xrpl-x402-client>=0.1.1` so clean installs resolve the MCP-compatible XRPL dependency set.

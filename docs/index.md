# Open XRPL x402 Stack 0.2.0

This project supplies the missing Python XRPL mechanisms for the official x402
2.20.0 runtime. It supports canonical protocol v2 only.

## Components

| Component | Responsibility |
| --- | --- |
| Core | Official schema re-exports and XRPL-specific types/helpers |
| Client | Build and sign exact XRP/IOU sequence or ticket payments |
| Middleware | HTTP/MCP challenge, verification, handler, settlement, response release |
| Facilitator | Strict verification, validated settlement, Redis replay/idempotency |
| Payer | HTTP, CLI, proxy, MCP payment and standard receipts |

## Protocol commitments

- Canonical v2 messages and HTTP headers
- CAIP-2 networks `xrpl:0`, `xrpl:1`, and `xrpl:2`
- Authorization flow: verify, handler, settle, respond
- Validated `tesSUCCESS` only; no optimistic success
- Durable transaction-hash reservation and pending reconciliation
- Unknown extensions preserved; Bazaar and payment-identifier supported
- Default recognized pegged assets capped at USD 1

Start with the [Testnet quickstart](quickstart/testnet-xrp.md), or read the
[0.1 to 0.2 migration guide](migration-0.2.md).

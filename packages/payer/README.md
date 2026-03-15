# xrpl-x402-payer

`xrpl-x402-payer` is the buyer-side CLI, proxy, and MCP package for the Open XRPL x402 Stack.

## Install

```bash
pip install xrpl-x402-payer
```

Install MCP support for Claude Desktop, Cursor, and other local agents:

```bash
pip install "xrpl-x402-payer[mcp]"
```

## CLI

```bash
xrpl-x402 pay https://merchant.example/premium --amount 0.001 --asset XRP --dry-run
xrpl-x402 proxy https://merchant.example --port 8787
xrpl-x402 skill install
xrpl-x402 mcp
```

`pay` sends one request, pays a valid x402 challenge when needed, and stores local receipts.

`proxy` runs a local forward proxy that auto-pays valid x402 challenges before retrying upstream.

`skill install` writes the bundled skill to `~/.agents/skills/xrpl-x402-payer/SKILL.md`.

`mcp` starts the stdio MCP server for Claude Desktop, Cursor, and compatible local agent runtimes.

## Claude Desktop / Cursor

```bash
claude mcp add xrpl-x402-payer -- xrpl-x402 mcp
```

Cursor can use the same command as a local MCP server.

## Environment

- `XRPL_WALLET_SEED`: wallet seed used for signing payments
- `XRPL_RPC_URL`: defaults to XRPL Testnet RPC
- `XRPL_NETWORK`: defaults to `xrpl:1`
- `XRPL_X402_RECEIPTS_PATH`: optional override for local receipt storage
- `XRPL_X402_MAX_SPEND`: optional default spend cap used by `budget_status`

## Public API

- `XRPLPayer`
- `PayResult`
- `ReceiptRecord`
- `budget_status`
- `get_receipts`
- `pay_with_x402`
- `create_proxy_app`

## Provenance

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

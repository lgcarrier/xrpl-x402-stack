# xrpl-x402-payer

[![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-payer?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-payer/)
[![Source directory](https://img.shields.io/badge/GitHub-packages%2Fpayer-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack/tree/main/packages/payer)

Use `xrpl-x402-payer` when you want a turnkey buyer experience instead of wiring the lower-level client SDK yourself.

## Install

```bash
pip install xrpl-x402-payer
```

Install MCP support for local agent runtimes:

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

`pay` sends a direct request, pays a valid x402 challenge, and records the result locally.

`proxy` starts a local forward proxy that retries valid x402 challenges automatically.

`skill install` installs the bundled payer skill into `~/.agents/skills/xrpl-x402-payer/`.

`mcp` starts the official stdio MCP server for Claude Desktop, Cursor, and compatible local agents.

## Claude Desktop / Cursor

```bash
claude mcp add xrpl-x402-payer -- xrpl-x402 mcp
```

## Environment

- `XRPL_WALLET_SEED`: wallet seed used for signing payments
- `XRPL_RPC_URL`: RPC endpoint for signing and autofill; when unset and `XRPL_NETWORK=xrpl:1`, the payer auto-selects a healthy public Testnet RPC
- `XRPL_NETWORK`: network id such as `xrpl:1`
- `XRPL_X402_MAX_SPEND`: optional default spend cap for local budget tracking
- `XRPL_X402_RECEIPTS_PATH`: optional receipt store override

Set `XRPL_RPC_URL` explicitly if you want to pin the payer to one provider instead of using automatic
Testnet selection.

## Public API

- `XRPLPayer`
- `pay_with_x402(...)`
- `budget_status(...)`
- `get_receipts(...)`
- `create_proxy_app(...)`

Use [xrpl-x402-client](client.md) when you want the lower-level SDK only. Use `xrpl-x402-payer` when you want the turnkey CLI, proxy, skill, and MCP workflow.

# xrpl-x402-payer

Use this skill when an agent needs to pay for a `402 Payment Required` API or dataset over XRPL x402.

## Install

```bash
xrpl-x402 skill install
```

That installs this skill into `~/.agents/skills/xrpl-x402-payer/SKILL.md`.

## Shell Mode

Use the CLI for one-off requests:

```bash
xrpl-x402 pay https://merchant.example/premium --amount 0.001 --asset XRP
```

Use the local forward proxy when repeated requests should auto-pay:

```bash
xrpl-x402 proxy https://merchant.example --port 8787
```

## Native MCP Mode (Claude Desktop / Cursor)

```bash
pip install "xrpl-x402-payer[mcp]"
xrpl-x402 skill install
xrpl-x402 mcp
```

Claude Desktop can add the server directly:

```bash
claude mcp add xrpl-x402-payer -- xrpl-x402 mcp
```

Agents can call `pay_url` directly in MCP mode without shelling out to the CLI.

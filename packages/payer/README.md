# xrpl-x402-payer

CLI, HTTP proxy, and MCP payer for canonical XRPL x402 v2 resources.

```bash
xrpl-x402 pay https://merchant.example/premium \
  --asset XRP --max-spend 1000
```

Without explicit asset options, the upstream spend policy accepts recognized
pegged assets only and caps each payment at USD 1. XRP, USDC, and custom IOUs
require explicit issuer-aware `XRPLAssetSpendLimit` entries and caps.

Receipts persist the standard `SettleResponse` together with immutable accepted
requirements. Signed payloads and payment identifiers are kept only in a locked
attempt sidecar next to the receipt JSONL. The sidecar is atomically replaced
before a paid HTTP request or MCP tool call is dispatched. Matching callers
share one per-fingerprint claim, so concurrent processes reuse the exact payload.
`settlement_pending` is stored as pending and never counted as paid; paid
attempt payloads are pruned by subsequent journal activity after a short
concurrent-caller reuse window.

Install `xrpl-x402-payer[mcp]` and run `xrpl-x402 mcp` for the local payer tool
service. Direct MCP clients should wrap a raw initialized MCP session with
`wrap_mcp_client_with_xrpl_payment(..., recovery_scope=...)`. The required scope
must be a stable, non-secret label for the endpoint and authenticated principal,
for example `https://mcp.example/sse|principal:user-42`; only its SHA-256 digest
is persisted. Never put a bearer token, API key, cookie, or wallet seed in the
scope.

```python
paid = wrap_mcp_client_with_xrpl_payment(
    raw_session,
    signer,
    recovery_scope="https://mcp.example/sse|principal:user-42",
)
result = await paid.call_tool("generate_report", {"topic": "XRPL"})
```

The upstream `create_x402_mcp_client` re-export remains available as a raw
convenience, but it does not use this package's local receipt/attempt journal and
therefore cannot provide the same crash or cross-process recovery guarantees.

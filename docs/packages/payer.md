# Payer package

`XRPLPayer` and the CLI use the official x402 v2 models, client lifecycle, and
canonical HTTP headers. A challenge is retried at most once and only when the
request body is replayable.

```bash
xrpl-x402 pay https://merchant.example/premium \
  --asset XRP --max-spend 1000
```

Default selection is restricted to recognized pegged assets and USD 1 per
payment. Explicit XRP, USDC, or custom IOU payments require asset, issuer where
applicable, and a cap.

Receipt records contain the standard `SettleResponse` and accepted
`PaymentRequirements`. Pending settlements are stored with `state: pending`,
a non-empty transaction hash, and an exact request fingerprint. They remain
excluded from paid budget totals. Receipt and budget scans cover the complete
JSONL history rather than a fixed recent window.

The signed `PaymentPayload` and payment identifier are transient recovery data,
not receipt fields. They live in `<receipts>.attempts.json`, protected by an
`fcntl` lock and atomic file replacement. The payload is durable before any paid
dispatch. Concurrent processes claim the same fingerprint and therefore never
sign or dispatch competing payloads. Pending/indeterminate attempts remain until
a terminal response; paid payloads remain briefly for concurrent waiters and are
pruned on subsequent journal activity after that reuse window.

HTTP fingerprints bind the payer account, canonical URL, method, body digest,
and caller-supplied headers. Header values contribute only to the digest and are
not copied into the journal.

MCP integration uses the official x402 lifecycle through `XRPLMCPClient`, which
records direct paid-tool receipts. `wrap_mcp_client_with_xrpl_payment` requires a
stable `recovery_scope` that identifies the server endpoint and authentication
principal. The fingerprint binds its SHA-256 digest with the payer, tool name,
arguments, and full challenged resource; the current resource and accepted
requirements are checked before a stored payload can be reused.

- `build_xrpl_payment_client`
- `wrap_mcp_client_with_xrpl_payment`
- `create_x402_mcp_client`

Use a non-secret subject, tenant, or principal label:

```python
paid = wrap_mcp_client_with_xrpl_payment(
    session,
    signer,
    recovery_scope="https://mcp.example/sse|principal:user-42",
)
```

Never use an access token, API key, session cookie, or wallet seed as the scope.
Only the scope digest is persisted. Calling the upstream
`create_x402_mcp_client` directly bypasses the local receipt/attempt wrapper and
does not provide its crash recovery guarantees.

The `xrpl-x402 mcp` command also provides local `pay_url`, receipt, budget, and
proxy tools.

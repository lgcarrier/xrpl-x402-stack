# xrpl-x402-middleware

Canonical x402 v2 HTTP and MCP resource-server integration for XRPL.

```python
from xrpl_x402_middleware import PaymentMiddlewareASGI, require_payment

app.add_middleware(
    PaymentMiddlewareASGI,
    routes={
        "GET /premium": require_payment(
            pay_to="rMerchant",
            network="xrpl:1",
            xrp_drops=1000,
            resource="https://merchant.example/premium",
            description="Premium data",
            service_name="Example merchant",
            tags=["premium", "xrpl"],
        )
    },
    facilitator_url="http://127.0.0.1:8000",
    bearer_token="replace-with-your-token",
)
```

The middleware verifies first, buffers the successful handler response, settles,
and only then releases protected bytes. Handler errors are not settled. Pending
settlements retry the identical facilitator envelope with bounded backoff.

Unsafe methods require the payment-identifier extension. Configure
`RedisResourceResponseStore` so a matching retry can recover the handler result
without repeating side effects. The store preserves exact response bytes and
retains them for at least `maxTimeoutSeconds` plus a safety margin (with a
600-second minimum by default), so a still-valid retry cannot repeat the
handler after cache expiry.

Before facilitator verification, the middleware rejects payload resource
metadata that does not match the authoritative HTTP route or MCP tool. Recovery
fingerprints also include the HTTP method, effective URL and query, and a digest
of the exact request body; MCP fingerprints include the exact tool name and a
digest of its canonical arguments. Raw request bodies and tool arguments are
not persisted in the binding record.

Use `create_xrpl_mcp_payment_wrapper` for paid MCP tools. It composes the
official x402 MCP wrapper, registers XRPL exact, preserves extension data, and
requires payment identifiers plus `RedisResourceResponseStore` for
non-idempotent tools. Install the helper with
`pip install "xrpl-x402-middleware[mcp]"`.

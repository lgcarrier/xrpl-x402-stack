# Middleware package

`PaymentMiddlewareASGI` composes the official resource-server and HTTP
transport. Configure route patterns with `require_payment`:

```python
app.add_middleware(
    PaymentMiddlewareASGI,
    routes={
        "POST /orders/*": require_payment(
            pay_to="rMerchant",
            network="xrpl:1",
            amount="0.25",
            asset="RLUSD",
            issuer="rIssuer",
            resource="https://merchant.example/orders",
            description="Create an order",
            service_name="Example orders",
            tags=["orders"],
        )
    },
    facilitator_url="http://127.0.0.1:8000",
    bearer_token="replace-with-your-token",
    response_store=response_store,
)
```

The order is verify, handler, settlement, response. Streaming output is fully
buffered. Handler failures and 4xx/5xx responses are not settled. For a
`settlement_pending` result, the upstream x402 resource server retries the
identical settlement envelope exactly once. If it remains pending, a later
identical request resumes settlement from the cached protected response without
rerunning the handler or exposing protected bytes early.

Unsafe methods automatically require payment-identifier. A
`RedisResourceResponseStore` preserves the successful handler output so a retry
resumes settlement without repeating side effects.

After settlement, `request.state.x402_payment` is an immutable
`XRPLPaymentContext` containing the standard settlement plus accepted
requirements context.

For MCP tools, use `create_xrpl_mcp_payment_wrapper`; non-idempotent mode
requires payment-identifier and `RedisResourceResponseStore` by default. Install
`xrpl-x402-middleware[mcp]` and see `examples/mcp_paid_tool.py`.

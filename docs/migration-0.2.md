# Migrate from 0.1.x to 0.2.0

Version 0.2.0 is an intentional clean break. Upgrade all five repository
packages together and pin the official runtime:

```text
x402==2.20.0
xrpl-x402-core==0.2.0
xrpl-x402-client==0.2.0
xrpl-x402-middleware==0.2.0
xrpl-x402-facilitator==0.2.0
xrpl-x402-payer==0.2.0
```

## Wire changes

Before, a requirement could contain a nested amount object and custom XRPL
metadata:

```json
{"maxAmountRequired":"1000","asset":{"assetId":"XRP:native"}}
```

Now it is an official `PaymentRequirements` object:

```json
{
  "scheme":"exact",
  "network":"xrpl:1",
  "asset":"XRP",
  "amount":"1000",
  "payTo":"rMerchant",
  "maxTimeoutSeconds":60,
  "extra":{"areFeesSponsored":false,"assetTransferMethod":"sequence"}
}
```

The client submits an official `PaymentPayload` with `accepted` and a scheme
payload containing only `signedTxBlob`. Facilitator calls use
`x402Version`, `paymentPayload`, and `paymentRequirements`. The standard
`VerifyResponse` and `SettleResponse` replace all custom response bodies.

Legacy bodies and snake-case protocol aliases are rejected. There is no body
fallback for HTTP: use the `PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, and
`PAYMENT-RESPONSE` base64 JSON headers.

## Registration

```python
from x402 import x402Client
from xrpl_x402_client import register_exact_xrpl_client

client = register_exact_xrpl_client(x402Client(), signer)
```

Resource servers normally use `PaymentMiddlewareASGI`; paid MCP tools use
`create_xrpl_mcp_payment_wrapper`. Both register the exact XRPL server mechanism.

## Asset and spend configuration

- XRP: `asset: "XRP"`, integer drops amount, no issuer.
- IOU: currency code in `asset`, account in `extra.issuer`, decimal amount.
- RLUSD uses the current official mainnet/testnet issuer table.
- The former Testnet RLUSD issuer causes a configuration error.
- XRP, USDC, and non-default IOUs require explicit payer allowlists and caps.

For those explicit assets, configure the issuer-aware cap on the upstream
client directly:

```python
from xrpl_x402_client import (
    XRPLAssetSpendLimit,
    apply_xrpl_spend_limits,
)

limits = [
    XRPLAssetSpendLimit(network="xrpl:1", asset="XRP", max_amount="1000000")
]
apply_xrpl_spend_limits(client, limits)
```


## Tickets

Set `extra.assetTransferMethod` to `ticketSequence` in the accepted requirement.
The payer uses `Sequence: 0` plus an unconsumed `TicketSequence`. Ticket creation
is opt-in; inventory targets are capped at 250.

Configure a nonzero inventory target to opt into ticket creation:

```python
from xrpl_x402_client import XRPLPaymentSigner

signer = XRPLPaymentSigner(
    wallet,
    network="xrpl:1",
    ticket_inventory_target=8,
)
```

A target of `0` disables automatic creation and requires an existing ticket;
targets above `250` are rejected.

## Extensions and idempotency

Unknown extension entries are echoed unchanged. Unsafe HTTP methods and
non-idempotent MCP tools require payment-identifier by default. Reuse the same
identifier and signed payload while reconciling a pending settlement. Reusing
an identifier for different requirements returns HTTP 409.

For unsafe HTTP routes, connect the existing Redis service to the response
store and leave Bazaar enabled:

```python
from redis.asyncio import Redis
from xrpl_x402_middleware import (
    PaymentMiddlewareASGI,
    RedisResourceResponseStore,
)

redis = Redis.from_url("redis://127.0.0.1:6379/0")
response_store = RedisResourceResponseStore(redis)
app.add_middleware(
    PaymentMiddlewareASGI,
    routes={"POST /orders": route_config},
    response_store=response_store,
    enable_bazaar=True,
)
```

This automatically requires the payment-identifier extension for the route

## Settlement and receipts

Remove `SETTLEMENT_MODE` entirely; startup fails if it is present. A payment is
successful only after a validated `tesSUCCESS`. `settlement_pending` contains a
transaction hash and network and must be retried with the identical envelope.

Receipts now store the standard `SettleResponse` plus immutable accepted
requirements and an exact HTTP/MCP request fingerprint. Signed payloads and
payment identifiers moved to the atomic attempt sidecar beside the receipt file;
they are no longer embedded in terminal or pending receipt records. Pending
receipts are not paid receipts.

Direct MCP callers must now supply a stable recovery scope:

```python
paid = wrap_mcp_client_with_xrpl_payment(
    session,
    signer,
    recovery_scope="https://mcp.example/sse|principal:user-42",
)
```

Use a non-secret endpoint/principal label. Do not use the authentication token
itself; the wrapper persists only the scope's SHA-256 digest.

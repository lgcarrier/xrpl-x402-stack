# xrpl-x402-facilitator

[![PyPI version](https://img.shields.io/pypi/v/xrpl-x402-facilitator?logo=pypi&logoColor=white)](https://pypi.org/project/xrpl-x402-facilitator/)
[![Source directory](https://img.shields.io/badge/GitHub-packages%2Ffacilitator-181717?logo=github&logoColor=white)](https://github.com/lgcarrier/xrpl-x402-stack/tree/main/packages/facilitator)

Install:

```bash
pip install xrpl-x402-facilitator
```

Use `xrpl-x402-facilitator` when you need a self-hosted verifier/settler for presigned XRPL payments.

## Public Entry Points

- `create_app(...)`
- `xrpl_x402_facilitator.main:app`
- `xrpl-x402-facilitator`

## Stable HTTP Contract

- `GET /health`
- `GET /supported`
- `POST /verify`
- `POST /settle`

## Run It

```bash
export MY_DESTINATION_ADDRESS=rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe
export FACILITATOR_BEARER_TOKEN=replace-with-your-token
export REDIS_URL=redis://127.0.0.1:6379/0
export NETWORK_ID=xrpl:1
export XRPL_RPC_URL=https://s.altnet.rippletest.net:51234
xrpl-x402-facilitator --host 127.0.0.1 --port 8000
```

Redis is required in every mode because the facilitator uses it for replay protection and rate limiting.

## Operating Modes

### Gateway Authentication

`GATEWAY_AUTH_MODE` controls how sellers authenticate to `POST /verify` and `POST /settle`.

- `single_token`: every seller shares `FACILITATOR_BEARER_TOKEN`
- `redis_gateways`: bearer tokens are looked up in Redis and tied to a specific `gateway_id`

Use `single_token` for local development and single-merchant deployments. Use `redis_gateways` when you want per-gateway isolation, Redis-backed token status, and stricter freshness checks.

### Settlement

`SETTLEMENT_MODE` controls what counts as success after `POST /settle`.

- `validated`: submit the transaction, poll XRPL until it is validated, and return `status="validated"`
- `optimistic`: submit the transaction and return immediately with `status="submitted"`

`validated` is the safer default for public deployments. `optimistic` is useful when downstream systems are already tracking validation and you want lower request latency.

## Key Configuration

- `XRPL_RPC_URL`: XRPL JSON-RPC endpoint used for `SubmitOnly`, `Tx`, and validated-ledger checks
- `MY_DESTINATION_ADDRESS`: the only destination address the facilitator will accept
- `NETWORK_ID`: CAIP-2 XRPL network identifier such as `xrpl:0` or `xrpl:1`
- `VALIDATION_TIMEOUT`: how long `validated` settlement mode waits for XRPL validation
- `MIN_XRP_DROPS`: minimum acceptable XRP exact payment
- `ALLOWED_ISSUED_ASSETS`: comma-separated extra assets in `CODE:ISSUER` form
- `MAX_REQUEST_BODY_BYTES`: hard request-size limit for `POST /verify` and `POST /settle`
- `REPLAY_PROCESSED_TTL_SECONDS`: how long processed invoice/blob replay markers stay in Redis
- `MAX_PAYMENT_LEDGER_WINDOW`: maximum future `LastLedgerSequence` window in `redis_gateways` mode
- `ENABLE_API_DOCS`: exposes `/docs`, `/redoc`, and `/openapi.json` when `true`

The full operator baseline is also shown in the repo-root `.env.example`.

## Supported Assets

`GET /supported` is the discovery endpoint for what a facilitator instance will accept.

The facilitator always supports XRP. It also includes built-in RLUSD and USDC issuers for `xrpl:0` and `xrpl:1`, and appends any extra `ALLOWED_ISSUED_ASSETS` entries after normalizing duplicates.

Example response:

```json
{
  "network": "xrpl:1",
  "assets": [
    {"code": "XRP"},
    {"code": "RLUSD", "issuer": "rnEVYfAWYP5HpPaWQiPSJMyDeUiEJ6zhy2"},
    {"code": "USDC", "issuer": "rHuGNhqTG32mfmAvWA8hUyWRLV3tCSwKQt"}
  ],
  "settlement_mode": "validated"
}
```

The seller middleware caches this response, and the optional `x402` server adapter surfaces the same information through `SupportedKind.extra.xrpl`.

## HTTP Contract

### `GET /health`

Health is intentionally small and stable:

```json
{
  "status": "healthy",
  "network": "xrpl:1"
}
```

### `GET /supported`

This endpoint requires no authentication and returns the network, supported assets, and settlement mode.

### `POST /verify`

Request body:

```json
{
  "signed_tx_blob": "120000228000000024...",
  "invoice_id": "A7F9C76B2EAC41A9B2D500AA76B8FA18"
}
```

Successful response:

```json
{
  "valid": true,
  "invoice_id": "A7F9C76B2EAC41A9B2D500AA76B8FA18",
  "amount": "0.001 XRP",
  "asset": {"code": "XRP"},
  "amount_details": {
    "value": "1000",
    "unit": "drops",
    "asset": {"code": "XRP"},
    "drops": 1000
  },
  "payer": "rBuyerAddress...",
  "destination": "rMerchantAddress...",
  "message": "Payment valid"
}
```

`/verify` confirms that the blob is a signed XRPL `Payment`, the asset and amount satisfy policy, the destination matches `MY_DESTINATION_ADDRESS`, and the payment is not already reserved or processed.

### `POST /settle`

Request body is identical to `/verify`.

Successful validated response:

```json
{
  "settled": true,
  "tx_hash": "A1B2C3...",
  "status": "validated"
}
```

Successful optimistic response:

```json
{
  "settled": true,
  "tx_hash": "A1B2C3...",
  "status": "submitted"
}
```

`/settle` reserves the payment against replay, submits the transaction to XRPL, and returns either a validated or submitted settlement state depending on `SETTLEMENT_MODE`.

## Status Codes And Errors

- `200`: successful `GET /health`, `GET /supported`, `POST /verify`, or `POST /settle`
- `400`: malformed request, including missing `signed_tx_blob`
- `401`: missing or invalid bearer token on payment endpoints
- `402`: the payment was rejected by facilitator policy or settlement failed
- `413`: request body exceeded `MAX_REQUEST_BODY_BYTES`
- `429`: Redis-backed rate limiter rejected the request

The most useful `402` categories are:

- invalid signature or malformed XRPL transaction
- wrong destination address
- partial payment flag set
- XRP below `MIN_XRP_DROPS`
- unsupported issued asset
- invoice/blob replay detected
- freshness failures in `redis_gateways` mode
- XRPL submission or validation failure during settlement

## Auth, Replay, And Freshness Notes

- `POST /verify` is limited to `30/minute`
- `POST /settle` is limited to `20/minute`
- rate limits are keyed by authenticated `gateway_id` when available, otherwise by client IP
- replay protection is tracked by both `invoice_id` and signed-blob hash
- `redis_gateways` mode additionally requires `LastLedgerSequence` and rejects stale or overly-far-future transactions

See [Header Contract](../how-it-works/header-contract.md) for the wire format that middleware and client exchange, and [Replay And Freshness](../how-it-works/replay-and-freshness.md) for the Redis and XRPL timing rules behind facilitator decisions.

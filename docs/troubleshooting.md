# Troubleshooting

## Facilitator startup rejects `SETTLEMENT_MODE`

Remove the variable. Version 0.2.0 has one settlement policy: validated
`tesSUCCESS` only.

## The buyer still receives HTTP 402

- Confirm merchant and facilitator bearer tokens match.
- Confirm the payer and requirement use the same CAIP-2 XRPL network.
- For XRP, set `PAYMENT_ASSET=XRP` and an integer-drops
  `PAYMENT_MAX_SPEND`.
- For an IOU, also set `PAYMENT_ASSET_ISSUER`; it must match
  `extra.issuer` exactly.
- Inspect `PAYMENT-REQUIRED` and ensure the response has no legacy body fields.

## Invoice or destination tag mismatch

The requirement advertises `extra.invoiceId` and/or `extra.destinationTag`.
The signer hashes the invoice text with SHA-256 for XRPL `InvoiceID` and copies
the tag exactly. Do not put invoice IDs beside `signedTxBlob` in the payload.

## `settlement_pending`

Retry the identical settlement envelope. Keep the same payment identifier and
signed payload. Do not create a replacement transaction and do not rerun an
unsafe handler. The facilitator reconciles the reserved hash without
rebroadcasting.

## Payment identifier conflict (HTTP 409)

The identifier was already bound to different accepted requirements or
resource data. Generate a new identifier for the new logical operation.

## Issued asset rejected

RLUSD must use the current official issuer. The former Testnet issuer is
rejected explicitly. USDC and custom IOUs must be present in
`ALLOWED_ISSUED_ASSETS=CODE:ISSUER` on the facilitator and in an explicit
issuer-aware payer spend limit.

## Simulation unavailable

The facilitator performs targeted XRP balance or IOU trust-line checks and
returns `extra.verificationPath: targetedChecks`. Treat repeated simulation
outages as degraded infrastructure even when targeted checks pass.

## Testnet helper problems

Pin `XRPL_TESTNET_RPC_URL` when public endpoints are unhealthy. For a wallet
created with `--new-wallet`, rerun `devtools.rlusd_fund` with the standalone
wallet file printed by the first invocation. For the cached demo buyer, rerun
the same command without `--wallet-file`; its multi-wallet cache is not accepted
as a standalone wallet file. The command reconciles any journaled transaction
before signing another one. Exit status `3` means the XRP faucet or ledger
transaction is still pending. For
USDC, complete the Circle faucet transfer and rerun `devtools.usdc_topup`;
report USDC acceptance as unavailable until the wallet is actually funded.

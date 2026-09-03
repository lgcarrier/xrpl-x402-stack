# Client package

`ExactXRPLClientScheme` implements the official x402 client mechanism contract.
Register it through `register_exact_xrpl_client` or use the provided httpx
transport.

The signer supports:

- account sequence payments
- ticket payments using `Sequence: 0` and `TicketSequence`
- opt-in ticket inventory creation, capped at 250
- timeout-derived `LastLedgerSequence`
- `NetworkID` for custom `xrpl:<id>` networks
- SHA-256 `InvoiceID` and exact `DestinationTag`
- XRP and IOU `Amount`/`SendMax` construction

```python
from xrpl_x402_client import (
    XRPLAssetSpendLimit,
    wrap_httpx_with_xrpl_payment,
)

limits = [XRPLAssetSpendLimit(
    network="xrpl:1", asset="XRP", max_amount="1000"
)]
client = wrap_httpx_with_xrpl_payment(signer, asset_limits=limits)
```

The transport retries only immutable in-memory request bodies. Custom async
streams are not buffered or retried automatically; if the server returns a
`402`, the caller receives that response and must decide how to recreate the
body safely.

Without explicit limits, use upstream spend controls for recognized pegged
assets with a USD 1 cap.

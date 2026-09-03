# Replay, freshness, and uncertainty

The signed XRPL transaction hash is the durable replay key. Redis reserves it
atomically across facilitator instances before submission and retains the
reservation through submission errors/timeouts until `LastLedgerSequence` has
passed.

An identical retry reconciles the existing transaction by hash. It does not
rebroadcast. A different resource/payment fingerprint using the same hash is a
business failure.

Payment identifiers add resource-level idempotency. Each identifier is scoped
to network and payee and bound to normalized accepted requirements/resource
data. Matching retries return cached results; mismatches return HTTP 409.

Freshness checks include current sequence or unconsumed ticket, current ledger,
bounded `LastLedgerSequence`, and a configured fee ceiling. Indeterminate
confirmation returns `settlement_pending`, never success.

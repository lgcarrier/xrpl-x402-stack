# xrpl-x402-middleware

`xrpl-x402-middleware` protects ASGI and FastAPI routes with exact XRPL x402 payments.

Public seller-facing APIs:

- `PaymentMiddlewareASGI`
- `require_payment(...)`
- `XRPLFacilitatorClient`

Optional `x402` interoperability helpers live under `xrpl_x402_middleware.adapters`.

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

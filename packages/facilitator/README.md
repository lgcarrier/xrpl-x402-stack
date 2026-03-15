# xrpl-x402-facilitator

`xrpl-x402-facilitator` is the FastAPI facilitator service in the Open XRPL x402 Stack.

It keeps the current facilitator HTTP contract stable:

- `GET /health`
- `GET /supported`
- `POST /verify`
- `POST /settle`

The package exposes `create_app(...)`, `xrpl_x402_facilitator.main:app`, and the `xrpl-x402-facilitator` CLI entrypoint.

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

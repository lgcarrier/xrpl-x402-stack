# xrpl-x402-facilitator

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

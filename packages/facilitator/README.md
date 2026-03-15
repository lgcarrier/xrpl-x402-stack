# xrpl-x402-facilitator

`xrpl-x402-facilitator` is the FastAPI verifier/settler service in the Open XRPL x402 Stack.

## Install

```bash
pip install xrpl-x402-facilitator
```

## Run

The CLI starts `xrpl_x402_facilitator.main:app`:

```bash
export MY_DESTINATION_ADDRESS=rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe
export FACILITATOR_BEARER_TOKEN=replace-with-your-token
export REDIS_URL=redis://127.0.0.1:6379/0
xrpl-x402-facilitator --host 127.0.0.1 --port 8000
```

Minimal import surface:

```python
from xrpl_x402_facilitator import create_app

app = create_app()
```

## Stable HTTP Contract

- `GET /health`
- `GET /supported`
- `POST /verify`
- `POST /settle`

## Public API

- `create_app(...)`
- `xrpl_x402_facilitator.main:app`
- `xrpl-x402-facilitator`

## Compatibility

- Python `3.12`
- `xrpl-py==4.5.0`
- Default network is `xrpl:0`; local demos commonly target `xrpl:1`
- `x402` is not required to run the facilitator service

## Provenance

The implementation is independently developed for the open `x402` protocol and does not copy `x402-xrpl`.

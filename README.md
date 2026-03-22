# Open XRPL x402 Stack

[![Docs](https://img.shields.io/badge/docs-live-0A7E3B)](https://lgcarrier.github.io/xrpl-x402-stack/)

Python-first XRPL infrastructure for the open `x402` protocol.

Hosted docs: <https://lgcarrier.github.io/xrpl-x402-stack/>

## Packages

- `xrpl-x402-core`: shared XRPL/x402 models, codecs, and exact-payment helpers
- `xrpl-x402-facilitator`: FastAPI verifier/settler service
- `xrpl-x402-middleware`: seller-side ASGI route protection
- `xrpl-x402-client`: buyer-side payment signing and `402` retry support
- `xrpl-x402-payer`: buyer CLI, proxy, receipts, and MCP server

## Fastest Real Demos

### XRP

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m devtools.quickstart
docker compose --env-file .env.quickstart up --build
docker compose --env-file .env.quickstart --profile demo run --rm buyer
```

That flow runs a real XRP payment on XRPL Testnet. `devtools.quickstart` auto-selects a
healthy public Testnet RPC and writes it to `.env.quickstart` as `XRPL_RPC_URL`.
Override the quickstart-side selection with `XRPL_TESTNET_RPC_URL` or
`python -m devtools.quickstart --xrpl-rpc-url ...`.
The generated buyer seed and facilitator token are written to `.env.quickstart`
and only echoed to stdout in redacted form.
The quickstart wallet cache now keeps one shared merchant wallet plus separate
buyer wallets for XRP, RLUSD, and USDC so the derived demo env files can run in
parallel without sharing XRPL sequence numbers.
The demo buyer output is recording-friendly by default and now shows the x402
challenge, the invoice id and XRPL fee used for signing, wallet A and wallet B
balances before and after the payment, the x402 payment response, and the final
HTTP/invoice/tx-hash summary.
`demo.run.sh` now assigns unique `FACILITATOR_PORT` and `MERCHANT_PORT` values
per run so isolated demo executions do not collide on host ports.
After a batch of demo runs, rebalance issued-asset funds back into the
contract-referenced buyer wallets with:

```bash
python -m devtools.demo_rebalance --contract demo.contract.json
```

If you also want to sweep merchant XRP above a fixed floor back to the XRP demo
buyer wallet, run:

```bash
python -m devtools.demo_rebalance --contract demo.contract.json --rebalance-xrp --merchant-xrp-floor 100
```

### RLUSD

Run this after the XRP quickstart is already working:

```bash
python -m devtools.rlusd_topup
python -m devtools.demo_env --asset rlusd
docker compose --env-file .env.quickstart.rlusd up --build
docker compose --env-file .env.quickstart.rlusd --profile demo run --rm buyer
```

If the cached demo wallet is already funded with RLUSD, you can skip the top-up
helper and start at `python -m devtools.demo_env --asset rlusd`. See the
[RLUSD guide](docs/asset-guides/rlusd.md) for the full faucet and recovery flow.
`devtools.demo_env --asset rlusd` now writes the RLUSD buyer seed into
`.env.quickstart.rlusd` so the RLUSD run signs with its own wallet.
If the demo trace shows the shared merchant wallet holding RLUSD while the
buyer wallet has `0`, rerun `python -m devtools.rlusd_topup` to bridge funds
into the dedicated RLUSD buyer wallet before retrying the demo.

### USDC

Run this after the XRP quickstart is already working:

```bash
python -m devtools.usdc_topup
# if prompted for a manual Circle faucet claim, complete it and rerun once
python -m devtools.usdc_topup
python -m devtools.demo_env --asset usdc
docker compose --env-file .env.quickstart.usdc up --build
docker compose --env-file .env.quickstart.usdc --profile demo run --rm buyer
```

If the cached demo wallet is already funded with USDC, you can skip the top-up
helper and start at `python -m devtools.demo_env --asset usdc`. See the
[USDC guide](docs/asset-guides/usdc.md) for the full faucet and recovery flow.
`devtools.demo_env --asset usdc` now writes the USDC buyer seed into
`.env.quickstart.usdc` so the USDC run signs with its own wallet.
If the demo trace shows the shared merchant wallet holding USDC while the
buyer wallet has `0`, rerun `python -m devtools.usdc_topup` to bridge funds
into the dedicated USDC buyer wallet before retrying the demo.

## Full AI Agent Support

```bash
pip install "xrpl-x402-payer[mcp]"
xrpl-x402 skill install
xrpl-x402 mcp
```

Claude Desktop and Cursor can add the payer directly with:

```bash
claude mcp add xrpl-x402-payer -- xrpl-x402 mcp
```

## Verification

```bash
pytest -q
for package in packages/core packages/facilitator packages/middleware packages/client packages/payer; do
  (
    cd "$package"
    python -m build --sdist
    python -m build --wheel
  )
done
twine check packages/*/dist/*
pip install -r docs/requirements.txt
mkdocs build --strict
PYTHONPYCACHEPREFIX=/tmp/pycache python -m compileall packages tests examples devtools
docker build -t xrpl-x402-facilitator .
```

See [docs/release.md](docs/release.md) for release steps and [CHANGELOG.md](CHANGELOG.md) for release notes.

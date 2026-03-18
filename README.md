# Open XRPL x402 Stack

Python-first XRPL infrastructure for the open `x402` protocol.

Hosted docs: <https://lgcarrier.github.io/xrpl-x402-stack/>

## Packages

- `xrpl-x402-core`: shared XRPL/x402 models, codecs, and exact-payment helpers
- `xrpl-x402-facilitator`: FastAPI verifier/settler service
- `xrpl-x402-middleware`: seller-side ASGI route protection
- `xrpl-x402-client`: buyer-side payment signing and `402` retry support
- `xrpl-x402-payer`: buyer CLI, proxy, receipts, and MCP server

## Fastest Real Demo

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
The hosted docs also include follow-on RLUSD and USDC guides.

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

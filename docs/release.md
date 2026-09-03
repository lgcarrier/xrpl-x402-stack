# Release 0.2.0

All five packages are released together at 0.2.0. The stable protocol runtime
is exactly `x402==2.21.0`; do not relax this pin in release artifacts.

## Required validation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -r docs/requirements.txt
python -m pip check
pytest -q
python -m compileall packages tests examples devtools
mkdocs build --strict
```

Build and inspect every package:

```bash
for package in core client middleware facilitator payer; do
  (cd "packages/$package" && python -m build --sdist --wheel)
done
twine check packages/*/dist/*
XRPL_X402_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
  pytest -m redis -q tests/test_redis_settlement_v2.py
docker compose config --quiet
docker build -t xrpl-x402-facilitator:0.2.0 .
```

CI also installs the built wheels into clean virtual environments, exercises
the core transaction hash against the committed TypeScript fixture, and runs
the complete canonical-header and cross-SDK fixture tests in the test job.

## Live Testnet acceptance

Live tests are opt-in:

```bash
RUN_XRPL_TESTNET_LIVE=1 pytest -m live -q
RUN_XRPL_TESTNET_LIVE=1 RUN_XRPL_TESTNET_USDC=1 \
  pytest -m live -q -k usdc
```

Run XRP and RLUSD for sequence and ticket payments. Run USDC when the funded
wallet/trust line is available; if unavailable, report it explicitly rather
than marking it passed.

## Publish order

Publish core, client, middleware, facilitator, and payer 0.2.0 artifacts as one
release window. Verify each TestPyPI installation before production upload.
Create release tags only after all artifacts and dependency resolution pass.

The weekly compatibility workflow is non-blocking and probes the newest x402
source for drift. It does not change the stable release pin.

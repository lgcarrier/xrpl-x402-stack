# Contributing

Thanks for contributing to `xrpl-x402-facilitator`.

## Development Setup

This project targets **Python 3.12**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Run the local test suite before opening a pull request:

```bash
pytest
```

If your change affects XRPL settlement, replay protection, ledger submission or
validation, buyer signing, or the live-test tooling, also run the full
payment-path check:

```bash
RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s
```

For routine changes outside those areas, the live XRPL Testnet test remains
opt-in because it depends on external network availability and faucet funding.

## Package Releases

This repo publishes four packages independently:

- `xrpl-x402-core`
- `xrpl-x402-facilitator`
- `xrpl-x402-middleware`
- `xrpl-x402-client`

Recommended verification before publishing:

```bash
for package in packages/core packages/facilitator packages/middleware packages/client; do
  (cd "$package" && python -m build)
done
twine check packages/*/dist/*
```

Release flow:

- Run the `Publish Python Package` workflow manually for a TestPyPI dry run.
- Push one of these tag prefixes to publish to PyPI:
  - `core-v*`
  - `facilitator-v*`
  - `middleware-v*`
  - `client-v*`

## Project Expectations

- Keep changes focused and easy to review.
- Preserve the non-custodial, stateless design unless a change explicitly
  requires broader architecture discussion.
- Add or update tests when behavior changes.
- Prefer simple, explicit behavior over framework-heavy abstraction.
- Document operator-facing changes in `README.md`.

## Pull Requests

Before opening a pull request, make sure:

- the default test suite passes locally
- the live XRPL Testnet flow also passes locally when you changed settlement,
  replay protection, ledger submission or validation, buyer signing, or
  live-test tooling
- Docker build still succeeds when container behavior changed
- new configuration or behavior is documented
- your change does not accidentally weaken replay protection or settlement
  checks

Small PRs are preferred over large cross-cutting rewrites.

## Issues

Bug reports and feature requests are welcome through GitHub Issues.

For security-sensitive issues, do **not** open a public issue. Follow the
process in [SECURITY.md](SECURITY.md).

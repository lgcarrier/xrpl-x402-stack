# Contributing

Thanks for contributing to `xrpl-x402-facilitator`.

## Development Setup

This project targets **Python 3.12**.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
```

Run the local test suite before opening a pull request:

```bash
pytest
```

If your change affects XRPL settlement, replay protection, ledger submission or
validation, or the live-test tooling, also run the full payment-path check:

```bash
RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s
```

For routine changes outside those areas, the live XRPL Testnet test remains
opt-in because it depends on external network availability and faucet funding.

## Package Releases

The middleware is published to PyPI as `xrpl-x402-middleware` and imported as
`xrpl_x402_middleware`.

Before the first release, configure GitHub Actions Trusted Publishing for both
TestPyPI and PyPI to trust this repository.

Recommended verification before publishing:

```bash
python -m build
twine check dist/*
```

Release flow:

- Run the `Publish Python Package` workflow manually for a TestPyPI dry run.
- Push a version tag such as `v0.1.0` after TestPyPI looks correct to trigger
  the PyPI upload job.

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
  replay protection, ledger submission or validation, or live-test tooling
- Docker build still succeeds
- new configuration or behavior is documented
- your change does not accidentally weaken replay protection or settlement
  checks

Small PRs are preferred over large cross-cutting rewrites.

## Issues

Bug reports and feature requests are welcome through GitHub Issues.

For security-sensitive issues, do **not** open a public issue. Follow the
process in [SECURITY.md](SECURITY.md).

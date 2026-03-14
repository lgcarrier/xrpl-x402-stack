# AGENTS.md

## Purpose

- Define repository-specific instructions for coding agents working in this repo.
- Keep changes concrete, minimal, and production-oriented.
- Prefer exact runnable commands over generic advice.

## Repository Snapshot

- Project: `xrpl-x402-facilitator`
- Stack: `Python 3.12`, `FastAPI`, `xrpl-py`, `Docker`
- Entry point: `app.main:app`
- App factory: `app.factory:create_app`

## Setup Commands

- Create virtualenv: `python3.12 -m venv .venv`
- Activate virtualenv: `source .venv/bin/activate`
- Install runtime dependencies: `pip install -r requirements.txt`
- Install dev/test dependencies: `pip install -r requirements-dev.txt && pip install -e .`

## Common Commands

- Run local API: `uvicorn app.main:app --reload`
- Run tests: `pytest`
- Run live XRPL Testnet integration test: `RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s`
- Compile-check source: `PYTHONPYCACHEPREFIX=/tmp/pycache python -m compileall app src tests`
- Build package artifacts: `python -m build`
- Verify package metadata: `twine check dist/*`
- Build Docker image: `docker build -t xrpl-x402-facilitator .`
- Run Docker Compose stack: `docker compose up --build`

## Workflow Rules

- Read `README.md`, this file, and any nearby docs before making non-trivial changes.
- Keep the service non-custodial and stateless unless the task explicitly changes architecture.
- Do not introduce a database, queue, auth layer, or external service dependency unless requested.
- Keep replay-protection and settlement logic changes narrow and easy to audit.
- Update `README.md` when behavior, configuration, setup, or operator workflow changes.
- Add or update tests whenever request handling, settlement logic, or API responses change.
- Ask before destructive actions, history rewrites, or broad dependency churn.

## Testing Expectations

- Run `pytest` for normal changes.
- Keep the live XRPL Testnet test out of the default routine suite; it depends on external network and faucet availability.
- If changing XRPL settlement, replay protection, ledger submission or validation, or live-test tooling, run the full payment-path verification locally:
  - `pytest`
  - `RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s`
- If changing container setup, also verify:
  - `docker build -t xrpl-x402-facilitator .`

## Project-Specific Notes

- Environment variables are documented in `.env.example`.
- The local default test suite intentionally skips the live XRPL Testnet test unless `RUN_XRPL_TESTNET_LIVE=1`.
- No dedicated linter or formatter is configured yet; do not invent one in routine changes.
- CI is defined in `.github/workflows/ci.yml` and runs package build checks,
  `pytest`, plus a Docker build smoke test.
- The PyPI package is named `xrpl-x402-middleware` and imported as `xrpl_x402_middleware`.

## Pull Request Expectations

- Summarize user-visible behavior changes.
- List the verification commands you ran and their outcomes.
- Call out XRPL, settlement, replay-protection, or Docker risks explicitly if touched.

# AGENTS.md

## Purpose

- Define repository-specific instructions for coding agents working in this repo.
- Keep changes concrete, minimal, and production-oriented.
- Prefer exact runnable commands over generic advice.

## Repository Snapshot

- Project: `xrpl-x402-facilitator`
- Stack: `Python 3.12`, `FastAPI`, `xrpl-py`, `httpx`, `Docker`
- Monorepo packages:
  - `packages/core` -> `xrpl-x402-core`
  - `packages/facilitator` -> `xrpl-x402-facilitator`
  - `packages/middleware` -> `xrpl-x402-middleware`
  - `packages/client` -> `xrpl-x402-client`
- Facilitator entry point: `xrpl_x402_facilitator.main:app`
- Facilitator app factory: `xrpl_x402_facilitator.factory:create_app`

## Setup Commands

- Create virtualenv: `python3.12 -m venv .venv`
- Activate virtualenv: `source .venv/bin/activate`
- Install repo dev/test dependencies: `pip install -r requirements-dev.txt`
- Install runtime stack only: `pip install -r requirements.txt`

## Common Commands

- Run facilitator locally: `xrpl-x402-facilitator --reload`
- Run merchant example: `uvicorn examples.merchant_fastapi.app:app --reload --port 8010`
- Run tests: `pytest`
- Run live XRPL Testnet integration test: `RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s`
- Compile-check source: `PYTHONPYCACHEPREFIX=/tmp/pycache python -m compileall packages tests examples devtools`
- Build all package artifacts:
  - `for package in packages/core packages/facilitator packages/middleware packages/client; do (cd "$package" && python -m build); done`
- Verify package metadata: `twine check packages/*/dist/*`
- Build Docker image: `docker build -t xrpl-x402-facilitator .`
- Run Docker Compose stack: `docker compose up --build`

## Workflow Rules

- Read `README.md`, this file, and any nearby docs before making non-trivial changes.
- Keep the stack non-custodial and stateless unless the task explicitly changes architecture.
- Do not introduce a database, queue, auth layer, or external service dependency unless requested.
- Keep replay-protection and settlement logic changes narrow and easy to audit.
- Update `README.md` when behavior, configuration, setup, packaging, or operator workflow changes.
- Add or update tests whenever request handling, settlement logic, packaging, or API responses change.
- Ask before destructive actions, history rewrites, or broad dependency churn.

## Testing Expectations

- Run `pytest` for normal changes.
- Keep the live XRPL Testnet test out of the default routine suite; it depends on external network and faucet availability.
- If changing XRPL settlement, replay protection, ledger submission or validation, buyer signing, or live-test tooling, run the full payment-path verification locally:
  - `pytest`
  - `RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s`
- If changing container setup, also verify:
  - `docker build -t xrpl-x402-facilitator .`

## Project-Specific Notes

- Environment variables are documented in `.env.example`.
- The local default test suite intentionally skips the live XRPL Testnet test unless `RUN_XRPL_TESTNET_LIVE=1`.
- No dedicated linter or formatter is configured yet; do not invent one in routine changes.
- CI is defined in `.github/workflows/ci.yml` and runs per-package build checks, `pytest`, and a Docker build smoke test.
- Optional upstream `x402` compatibility is tested against a pinned version from `requirements-dev.txt`.

## Pull Request Expectations

- Summarize user-visible behavior changes.
- List the verification commands you ran and their outcomes.
- Call out XRPL, settlement, replay-protection, packaging, or Docker risks explicitly if touched.

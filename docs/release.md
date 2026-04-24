# Release Playbook

This repo publishes five Python packages with trusted publishing:

- `xrpl-x402-core`
- `xrpl-x402-facilitator`
- `xrpl-x402-middleware`
- `xrpl-x402-client`
- `xrpl-x402-payer`

## Trusted Publishing Setup

Create each project on both PyPI and TestPyPI, then add a trusted publisher with these settings:

- Owner: `lgcarrier`
- Repository: `xrpl-x402-stack`
- Workflow: `.github/workflows/publish-package.yml`
- Environment:
  - `testpypi-core` / `pypi-core` for `xrpl-x402-core`
  - `testpypi-facilitator` / `pypi-facilitator` for `xrpl-x402-facilitator`
  - `testpypi-middleware` / `pypi-middleware` for `xrpl-x402-middleware`
  - `testpypi-client` / `pypi-client` for `xrpl-x402-client`
  - `testpypi-payer` / `pypi-payer` for `xrpl-x402-payer`

Each package needs its own environment because PyPI/TestPyPI treat the GitHub OIDC identity as the combination of repository, workflow, and environment.

PyPI and TestPyPI currently allow only three pending trusted publishers at once. For the first release wave:

1. Register pending publishers for `core`, `facilitator`, and `middleware`.
2. Publish `core` on the target index.
3. Add the `client` pending publisher on that index.
4. Add the `payer` pending publisher on that index.
5. Continue with `facilitator`, `middleware`, `client`, and `payer`.

GitHub environments used by the workflow:

- `testpypi-core`
- `testpypi-facilitator`
- `testpypi-middleware`
- `testpypi-client`
- `testpypi-payer`
- `pypi-core`
- `pypi-facilitator`
- `pypi-middleware`
- `pypi-client`
- `pypi-payer`

## Version Prep

Current release line:

- `0.1.2` for `xrpl-x402-core` and `xrpl-x402-client`
- `0.1.1` for `xrpl-x402-facilitator` and `xrpl-x402-middleware`
- `0.1.3` for `xrpl-x402-payer`

Before publishing:

1. Update package versions in the relevant `packages/*/pyproject.toml` files if needed.
2. Update [CHANGELOG.md](https://github.com/lgcarrier/xrpl-x402-stack/blob/main/CHANGELOG.md).
3. Confirm dependency ranges still match the release order you intend to publish.

## Local Verification

Run the standard release checks:

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
PYTHONPYCACHEPREFIX=/tmp/pycache python -m compileall packages tests examples devtools
pip install -r docs/requirements.txt
mkdocs build --strict
docker build -t xrpl-x402-facilitator .
```

If settlement, replay protection, or signing changed, also run:

```bash
RUN_XRPL_TESTNET_LIVE=1 pytest -m live tests/integration/test_live_testnet.py -s
```

## TestPyPI Rehearsal

Run the `Publish Python Package` workflow manually for each package.

Recommended order:

1. `core`
2. `facilitator`
3. `middleware`
4. `client`
5. `payer`

If you use the GitHub CLI:

```bash
gh workflow run "Publish Python Package" -f package=core
gh workflow run "Publish Python Package" -f package=facilitator
gh workflow run "Publish Python Package" -f package=middleware
gh workflow run "Publish Python Package" -f package=client
gh workflow run "Publish Python Package" -f package=payer
```

The workflow publishes to TestPyPI for `workflow_dispatch` runs and verifies clean installs after publishing.

## Verify TestPyPI Installs

TestPyPI installs should use PyPI as an extra index for shared dependencies:

```bash
python3.12 -m venv /tmp/x402-testpypi
source /tmp/x402-testpypi/bin/activate
pip install --upgrade pip
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ xrpl-x402-core==0.1.2
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ xrpl-x402-facilitator==0.1.1
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "xrpl-x402-middleware[x402]==0.1.1"
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "xrpl-x402-client[x402]==0.1.2"
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "xrpl-x402-payer[mcp]==0.1.3"
```

## Production Publish

After TestPyPI succeeds, publish in this order:

```bash
git tag core-v0.1.2
git push origin core-v0.1.2
```

Wait for index availability, then publish the remaining packages:

```bash
git tag facilitator-v0.1.1
git push origin facilitator-v0.1.1

git tag middleware-v0.1.1
git push origin middleware-v0.1.1

git tag client-v0.1.2
git push origin client-v0.1.2

git tag payer-v0.1.3
git push origin payer-v0.1.3
```

The publish workflow fails if the tag version and the package `version` field do not match.

## Post-Publish Checks

After each PyPI publish:

1. Install the package into a clean virtualenv.
2. Verify the package smoke import or CLI.
3. Confirm the package README renders correctly on the PyPI project page.

Example clean install checks:

```bash
python3.12 -m venv /tmp/x402-pypi
source /tmp/x402-pypi/bin/activate
pip install --upgrade pip
pip install xrpl-x402-core==0.1.2
pip install xrpl-x402-facilitator==0.1.1
pip install "xrpl-x402-middleware[x402]==0.1.1"
pip install "xrpl-x402-client[x402]==0.1.2"
pip install "xrpl-x402-payer[mcp]==0.1.3"
```

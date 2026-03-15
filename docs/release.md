# Release Playbook

This repo publishes four Python packages with trusted publishing:

- `xrpl-x402-core`
- `xrpl-x402-facilitator`
- `xrpl-x402-middleware`
- `xrpl-x402-client`

## Trusted Publishing Setup

Create each project on both PyPI and TestPyPI, then add a trusted publisher with these settings:

- Owner: `lgcarrier`
- Repository: `xrpl-x402-stack`
- Workflow: `.github/workflows/publish-package.yml`
- Environment: `testpypi` for TestPyPI publishers, `pypi` for PyPI publishers

GitHub environments already used by the workflow:

- `testpypi`
- `pypi`

## Version Prep

Keep the first public release at `0.1.0` for all four packages.

Before publishing:

1. Update package versions in the relevant `packages/*/pyproject.toml` files if needed.
2. Update [CHANGELOG.md](https://github.com/lgcarrier/xrpl-x402-stack/blob/main/CHANGELOG.md).
3. Confirm dependency ranges still match the release order you intend to publish.

## Local Verification

Run the standard release checks:

```bash
pytest -q
for package in packages/core packages/facilitator packages/middleware packages/client; do
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

If you use the GitHub CLI:

```bash
gh workflow run "Publish Python Package" -f package=core
gh workflow run "Publish Python Package" -f package=facilitator
gh workflow run "Publish Python Package" -f package=middleware
gh workflow run "Publish Python Package" -f package=client
```

The workflow publishes to TestPyPI for `workflow_dispatch` runs and verifies clean installs after publishing.

## Verify TestPyPI Installs

TestPyPI installs should use PyPI as an extra index for shared dependencies:

```bash
python3.12 -m venv /tmp/x402-testpypi
source /tmp/x402-testpypi/bin/activate
pip install --upgrade pip
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ xrpl-x402-core==0.1.0
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ xrpl-x402-facilitator==0.1.0
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "xrpl-x402-middleware[x402]==0.1.0"
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "xrpl-x402-client[x402]==0.1.0"
```

## Production Publish

After TestPyPI succeeds, publish in this order:

```bash
git tag core-v0.1.0
git push origin core-v0.1.0
```

Wait for index availability, then publish the remaining packages:

```bash
git tag facilitator-v0.1.0
git push origin facilitator-v0.1.0

git tag middleware-v0.1.0
git push origin middleware-v0.1.0

git tag client-v0.1.0
git push origin client-v0.1.0
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
pip install xrpl-x402-core==0.1.0
pip install xrpl-x402-facilitator==0.1.0
pip install "xrpl-x402-middleware[x402]==0.1.0"
pip install "xrpl-x402-client[x402]==0.1.0"
```

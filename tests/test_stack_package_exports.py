import tomllib
from importlib.resources import files
from pathlib import Path

from xrpl_x402_client import (
    ExactXRPLClientScheme,
    XRPLPaymentSigner,
    build_payment_signature,
    wrap_httpx_with_xrpl_payment,
)
from xrpl_x402_core import (
    ExactXRPLPayload,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResponse,
)
from xrpl_x402_facilitator import ExactXRPLFacilitatorScheme, create_app
from xrpl_x402_middleware import (
    ExactXRPLServerScheme,
    PaymentMiddlewareASGI,
    XRPLFacilitatorClient,
    create_xrpl_mcp_payment_wrapper,
    require_payment,
)
from xrpl_x402_payer import (
    XRPLPayer,
    create_x402_mcp_client,
    pay_with_x402,
    wrap_mcp_client_with_xrpl_payment,
)


def test_stack_packages_export_expected_public_entrypoints() -> None:
    assert PaymentPayload is not None
    assert PaymentRequired is not None
    assert PaymentRequirements is not None
    assert SettleResponse is not None
    assert ExactXRPLPayload is not None
    assert ExactXRPLClientScheme is not None
    assert ExactXRPLServerScheme is not None
    assert ExactXRPLFacilitatorScheme is not None
    assert create_app is not None
    assert PaymentMiddlewareASGI is not None
    assert XRPLFacilitatorClient is not None
    assert require_payment is not None
    assert create_xrpl_mcp_payment_wrapper is not None
    assert XRPLPaymentSigner is not None
    assert build_payment_signature is not None
    assert wrap_httpx_with_xrpl_payment is not None
    assert XRPLPayer is not None
    assert pay_with_x402 is not None
    assert create_x402_mcp_client is not None
    assert wrap_mcp_client_with_xrpl_payment is not None


def test_typed_packages_ship_pep_561_markers() -> None:
    for package in (
        "xrpl_x402_core",
        "xrpl_x402_client",
        "xrpl_x402_middleware",
        "xrpl_x402_facilitator",
        "xrpl_x402_payer",
    ):
        assert files(package).joinpath("py.typed").is_file()


def test_middleware_declares_the_upstream_fastapi_runtime_extra() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads(
        (repository_root / "packages/middleware/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert "x402[extensions,fastapi]==2.21.0" in pyproject["project"][
        "dependencies"
    ]

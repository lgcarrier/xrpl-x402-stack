from xrpl_x402_middleware import (
    PaymentMiddlewareASGI,
    PaymentPayload,
    PaymentRequired,
    PaymentResponse,
    RouteConfig,
    XRPLAmount,
    XRPLAsset,
    XRPLFacilitatorClient,
    XRPLPaymentOption,
    XRPLPaymentPayload,
    require_payment,
)


def test_package_exports_public_api() -> None:
    assert PaymentMiddlewareASGI is not None
    assert PaymentPayload is not None
    assert PaymentRequired is not None
    assert PaymentResponse is not None
    assert RouteConfig is not None
    assert XRPLAmount is not None
    assert XRPLAsset is not None
    assert XRPLFacilitatorClient is not None
    assert XRPLPaymentOption is not None
    assert XRPLPaymentPayload is not None
    assert require_payment is not None


def test_require_payment_builds_route_config() -> None:
    route_config = require_payment(
        facilitator_url="https://facilitator.example",
        bearer_token="secret-token",
        pay_to="rDESTINATION123456789",
        network="xrpl:1",
        xrp_drops=1000,
        description="One paid request",
    )

    assert route_config.facilitator_url == "https://facilitator.example"
    assert route_config.accepts[0].asset.model_dump() == {"code": "XRP", "issuer": None}
    assert route_config.accepts[0].amount.model_dump() == {
        "value": "1000",
        "unit": "drops",
        "drops": 1000,
    }
    assert route_config.accepts[0].extra == {
        "xrpl": {
            "asset": {"code": "XRP"},
            "assetId": "XRP:native",
            "amount": {"value": "1000", "unit": "drops", "drops": 1000},
        }
    }

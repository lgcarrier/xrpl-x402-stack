from __future__ import annotations

import asyncio
import base64
import json
from typing import Callable

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from xrpl_x402_middleware.client import (
    FacilitatorSettleResponse,
    FacilitatorSupported,
    FacilitatorVerifyResponse,
)
from xrpl_x402_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorTransportError,
    RouteConfigurationError,
)
from xrpl_x402_middleware.middleware import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    PaymentMiddlewareASGI,
    require_payment,
)
from xrpl_x402_middleware.types import PaymentPayload, PaymentRequired, PaymentResponse, XRPLAmount, XRPLAsset
from xrpl_x402_middleware.utils import decode_model_from_base64, encode_model_to_base64

FACILITATOR_URL = "https://facilitator.example"
FACILITATOR_TOKEN = "secret-token"
DESTINATION = "rDESTINATION123456789"
PAYER = "rPAYER123456789"


class FakeFacilitatorClient:
    def __init__(
        self,
        *,
        supported: FacilitatorSupported,
        verify_response: FacilitatorVerifyResponse | None = None,
        settle_response: FacilitatorSettleResponse | None = None,
        verify_error: Exception | None = None,
        settle_error: Exception | None = None,
    ) -> None:
        self.supported = supported
        self.verify_response = verify_response
        self.settle_response = settle_response
        self.verify_error = verify_error
        self.settle_error = settle_error
        self.verify_calls: list[tuple[str, str | None]] = []
        self.settle_calls: list[tuple[str, str | None]] = []
        self.started = False
        self.closed = False

    async def startup(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def get_supported(self, *, force_refresh: bool = False) -> FacilitatorSupported:
        return self.supported

    async def verify_payment(
        self,
        *,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> FacilitatorVerifyResponse:
        self.verify_calls.append((signed_tx_blob, invoice_id))
        if self.verify_error is not None:
            raise self.verify_error
        if self.verify_response is None:
            raise AssertionError("verify_response must be configured")
        return self.verify_response

    async def settle_payment(
        self,
        *,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> FacilitatorSettleResponse:
        self.settle_calls.append((signed_tx_blob, invoice_id))
        if self.settle_error is not None:
            raise self.settle_error
        if self.settle_response is None:
            raise AssertionError("settle_response must be configured")
        return self.settle_response


def build_supported(*assets: XRPLAsset, network: str = "xrpl:1") -> FacilitatorSupported:
    return FacilitatorSupported(
        network=network,
        assets=list(assets),
        settlement_mode="validated",
    )


def build_verify_response(
    *,
    destination: str = DESTINATION,
    asset: XRPLAsset | None = None,
    amount: XRPLAmount | None = None,
) -> FacilitatorVerifyResponse:
    active_asset = asset or XRPLAsset(code="XRP", issuer=None)
    active_amount = amount or XRPLAmount(value="1000", unit="drops", drops=1000)
    return FacilitatorVerifyResponse(
        valid=True,
        invoice_id="INV-123",
        amount="0.001 XRP" if active_asset.code == "XRP" else f"{active_amount.value} {active_asset.code}",
        asset=active_asset,
        amount_details=active_amount,
        payer=PAYER,
        destination=destination,
        message="Payment valid",
    )


def build_settle_response() -> FacilitatorSettleResponse:
    return FacilitatorSettleResponse(
        settled=True,
        tx_hash="ABC123HASH",
        status="validated",
    )


def build_app(client_factory: Callable[[str, str], FakeFacilitatorClient], route_config=None) -> FastAPI:
    app = FastAPI()

    @app.get("/paid")
    async def paid(request: Request) -> dict[str, object]:
        payment = request.state.x402_payment
        return {
            "invoice_id": payment.invoice_id,
            "tx_hash": payment.tx_hash,
            "payer": payment.payer,
        }

    @app.get("/free")
    async def free() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": route_config
            or require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Paid route",
            )
        },
        client_factory=client_factory,
    )
    return app


def build_middleware(
    client_factory: Callable[[str, str], FakeFacilitatorClient],
    route_config=None,
) -> PaymentMiddlewareASGI:
    return PaymentMiddlewareASGI(
        FastAPI(),
        route_configs={
            "GET /paid": route_config
            or require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
            )
        },
        client_factory=client_factory,
    )


def build_payload_header(payload: PaymentPayload) -> dict[str, str]:
    return {PAYMENT_SIGNATURE_HEADER: encode_model_to_base64(payload)}


def make_client_factory(client: FakeFacilitatorClient) -> Callable[[str, str], FakeFacilitatorClient]:
    def _factory(url: str, token: str) -> FakeFacilitatorClient:
        assert url == FACILITATOR_URL
        assert token == FACILITATOR_TOKEN
        return client

    return _factory


def test_unpaid_request_returns_payment_required_header_and_body() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_response=build_verify_response(),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))

    with TestClient(app) as test_client:
        response = test_client.get("/paid")

    assert response.status_code == 402
    challenge = decode_model_from_base64(response.headers[PAYMENT_REQUIRED_HEADER], PaymentRequired)
    assert challenge.model_dump(by_alias=True, exclude_none=True) == response.json()
    assert challenge.accepts[0].network == "xrpl:1"
    assert challenge.accepts[0].pay_to == DESTINATION


@pytest.mark.parametrize(
    "header_value",
    [
        "%%%not-base64%%%",
        base64.b64encode(b'{"x402Version":1,"scheme":"exact","network":"xrpl:1","payload":{}}').decode("ascii"),
        base64.b64encode(
            b'{"x402Version":2,"scheme":"wrong","network":"xrpl:1","payload":{"signedTxBlob":"blob"}}'
        ).decode("ascii"),
    ],
)
def test_invalid_payment_signature_returns_fresh_challenge(header_value: str) -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_response=build_verify_response(),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers={PAYMENT_SIGNATURE_HEADER: header_value})

    assert response.status_code == 402
    assert PAYMENT_REQUIRED_HEADER in response.headers
    assert len(client.verify_calls) == 0


def test_network_mismatch_returns_fresh_challenge() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_response=build_verify_response(),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))
    payment_payload = PaymentPayload(
        network="xrpl:0",
        payload={"signedTxBlob": "signed-blob"},
    )

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers=build_payload_header(payment_payload))

    assert response.status_code == 402
    assert response.json()["error"] == "Payment network is not accepted for this route"
    assert len(client.verify_calls) == 0


@pytest.mark.parametrize(
    ("route_config", "verify_response"),
    [
        (
            require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to="rOTHERDESTINATION",
                network="xrpl:1",
                xrp_drops=1000,
            ),
            build_verify_response(),
        ),
        (
            require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=2000,
            ),
            build_verify_response(),
        ),
        (
            require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                amount="1.0",
                asset_code="RLUSD",
                asset_issuer="rRLUSDISSUER",
            ),
            build_verify_response(),
        ),
    ],
)
def test_mismatched_verified_payment_returns_fresh_challenge(
    route_config,
    verify_response: FacilitatorVerifyResponse,
) -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(
            XRPLAsset(code="XRP", issuer=None),
            XRPLAsset(code="RLUSD", issuer="rRLUSDISSUER"),
        ),
        verify_response=verify_response,
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client), route_config=route_config)
    payment_payload = PaymentPayload(
        network="xrpl:1",
        payload={"signedTxBlob": "signed-blob"},
    )

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers=build_payload_header(payment_payload))

    assert response.status_code == 402
    assert response.json()["error"] == "Submitted payment does not satisfy this route"
    assert len(client.settle_calls) == 0


def test_issued_asset_amount_match_tolerates_equivalent_decimal_formatting() -> None:
    route_config = require_payment(
        facilitator_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        pay_to=DESTINATION,
        network="xrpl:1",
        amount="2.50",
        asset_code="USDC",
        asset_issuer="rUSDCISSUER",
    )
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="USDC", issuer="rUSDCISSUER")),
        verify_response=build_verify_response(
            asset=XRPLAsset(code="USDC", issuer="rUSDCISSUER"),
            amount=XRPLAmount(value="2.5", unit="issued"),
        ),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client), route_config=route_config)
    payment_payload = PaymentPayload(
        network="xrpl:1",
        payload={"signedTxBlob": "signed-blob"},
    )

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers=build_payload_header(payment_payload))

    assert response.status_code == 200
    assert len(client.settle_calls) == 1


def test_verification_failures_are_normalized_to_payment_required() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_error=FacilitatorPaymentError("verify", 402, "Invalid payment: replay"),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))
    payment_payload = PaymentPayload(
        network="xrpl:1",
        payload={"signedTxBlob": "signed-blob"},
    )

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers=build_payload_header(payment_payload))

    assert response.status_code == 402
    assert response.json()["error"] == "Invalid payment: replay"


def test_transport_failures_return_service_unavailable() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_error=FacilitatorTransportError("Unable to reach facilitator"),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))
    payment_payload = PaymentPayload(
        network="xrpl:1",
        payload={"signedTxBlob": "signed-blob"},
    )

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers=build_payload_header(payment_payload))

    assert response.status_code == 503
    assert response.json() == {"detail": "Unable to reach facilitator"}


def test_successful_paid_request_injects_context_and_sets_payment_response_header() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_response=build_verify_response(),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))
    payment_payload = PaymentPayload(
        network="xrpl:1",
        payload={"signedTxBlob": "signed-blob", "invoiceId": "INV-123"},
    )

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers=build_payload_header(payment_payload))

    assert response.status_code == 200
    assert response.json() == {
        "invoice_id": "INV-123",
        "tx_hash": "ABC123HASH",
        "payer": PAYER,
    }
    payment_response = decode_model_from_base64(
        response.headers[PAYMENT_RESPONSE_HEADER],
        PaymentResponse,
    )
    assert payment_response.model_dump(by_alias=True, exclude_none=True) == json.loads(
        base64.b64decode(response.headers[PAYMENT_RESPONSE_HEADER]).decode("utf-8")
    )
    assert payment_response.tx_hash == "ABC123HASH"
    assert payment_response.invoice_id == "INV-123"
    assert client.verify_calls == [("signed-blob", "INV-123")]
    assert client.settle_calls == [("signed-blob", "INV-123")]


def test_non_protected_routes_pass_through() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_response=build_verify_response(),
        settle_response=build_settle_response(),
    )
    app = build_app(make_client_factory(client))

    with TestClient(app) as test_client:
        response = test_client.get("/free")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(client.verify_calls) == 0


def test_startup_validation_rejects_network_mismatch() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None), network="xrpl:0"),
        verify_response=build_verify_response(),
        settle_response=build_settle_response(),
    )
    middleware = build_middleware(make_client_factory(client))

    with pytest.raises(
        RouteConfigurationError,
        match="expects xrpl:1, but facilitator supports xrpl:0",
    ):
        asyncio.run(middleware.startup())


def test_startup_validation_rejects_unsupported_asset() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(XRPLAsset(code="XRP", issuer=None)),
        verify_response=build_verify_response(
            asset=XRPLAsset(code="RLUSD", issuer="rRLUSDISSUER"),
            amount=XRPLAmount(value="1.0", unit="issued"),
        ),
        settle_response=build_settle_response(),
    )
    route_config = require_payment(
        facilitator_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        pay_to=DESTINATION,
        network="xrpl:1",
        amount="1.0",
        asset_code="RLUSD",
        asset_issuer="rRLUSDISSUER",
    )
    middleware = build_middleware(make_client_factory(client), route_config=route_config)

    with pytest.raises(
        RouteConfigurationError,
        match="unsupported asset RLUSD:rRLUSDISSUER",
    ):
        asyncio.run(middleware.startup())

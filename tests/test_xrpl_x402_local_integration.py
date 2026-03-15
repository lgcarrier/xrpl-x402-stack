from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
import httpx
from slowapi import Limiter as SlowLimiter
from xrpl.wallet import Wallet

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_client import XRPLPaymentSigner, wrap_httpx_with_xrpl_payment
from xrpl_x402_core import PaymentResponse, decode_model_from_base64
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.factory import create_app
from xrpl_x402_facilitator.models import AssetDescriptor, SettleResponse, StructuredAmount, VerifyResponse
from xrpl_x402_middleware import XRPLFacilitatorClient
from xrpl_x402_middleware.middleware import PAYMENT_RESPONSE_HEADER, PaymentMiddlewareASGI, require_payment

FACILITATOR_TOKEN = "local-facilitator-token"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rLOCALPAYER123456789"
INVOICE_ID = "LOCAL-INVOICE-123"
TX_HASH = "LOCAL-TX-HASH-123"


class RecordingFacilitatorService:
    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str | None]] = []
        self.settle_calls: list[tuple[str, str | None]] = []

    def supported_assets(self) -> list[AssetDescriptor]:
        return [AssetDescriptor(code="XRP", issuer=None)]

    async def verify_payment(
        self,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> VerifyResponse:
        self.verify_calls.append((signed_tx_blob, invoice_id))
        return VerifyResponse(
            valid=True,
            invoice_id=invoice_id or INVOICE_ID,
            amount="0.001 XRP",
            asset=AssetDescriptor(code="XRP", issuer=None),
            amount_details=StructuredAmount(
                value="1000",
                unit="drops",
                asset=AssetDescriptor(code="XRP", issuer=None),
                drops=1000,
            ),
            payer=PAYER,
            destination=DESTINATION,
            message="Payment valid",
        )

    async def settle_payment(
        self,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> SettleResponse:
        self.settle_calls.append((signed_tx_blob, invoice_id))
        return SettleResponse(
            settled=True,
            tx_hash=TX_HASH,
            status="validated",
        )


def _create_app_with_in_memory_rate_limiter(
    *,
    app_settings: Settings,
    xrpl_service: RecordingFacilitatorService,
) -> FastAPI:
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return create_app(
            app_settings=app_settings,
            xrpl_service=xrpl_service,
        )
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


def test_middleware_uses_real_local_facilitator_instance() -> None:
    facilitator_service = RecordingFacilitatorService()
    facilitator_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL="https://s.altnet.rippletest.net:51234",
        MY_DESTINATION_ADDRESS=DESTINATION,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        FACILITATOR_BEARER_TOKEN=FACILITATOR_TOKEN,
    )
    facilitator_app = _create_app_with_in_memory_rate_limiter(
        app_settings=facilitator_settings,
        xrpl_service=facilitator_service,
    )

    middleware_app = FastAPI()

    @middleware_app.get("/paid")
    async def paid(request: Request) -> dict[str, str]:
        payment = request.state.x402_payment
        return {
            "invoice_id": payment.invoice_id,
            "tx_hash": payment.tx_hash,
            "payer": payment.payer,
        }

    async_facilitator_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url="http://facilitator.local",
    )

    middleware_app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": require_payment(
                facilitator_url="http://facilitator.local",
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Local facilitator integration route",
            )
        },
        client_factory=lambda facilitator_url, bearer_token: XRPLFacilitatorClient(
            base_url=facilitator_url,
            bearer_token=bearer_token,
            async_client=async_facilitator_client,
        ),
    )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )

    async def _make_paid_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=middleware_app)
        async with wrap_httpx_with_xrpl_payment(
            signer,
            transport=transport,
            base_url="http://merchant.local",
        ) as client:
            response = await client.get("/paid")
        await async_facilitator_client.aclose()
        return response

    response = asyncio.run(_make_paid_request())

    assert response.status_code == 200
    assert response.json() == {
        "invoice_id": INVOICE_ID,
        "tx_hash": TX_HASH,
        "payer": PAYER,
    }
    payment_response = decode_model_from_base64(
        response.headers[PAYMENT_RESPONSE_HEADER],
        PaymentResponse,
    )
    assert payment_response.invoice_id == INVOICE_ID
    assert payment_response.tx_hash == TX_HASH
    assert payment_response.payer == PAYER
    assert len(facilitator_service.verify_calls) == 1
    assert len(facilitator_service.settle_calls) == 1
    assert facilitator_service.verify_calls[0][1] is None
    assert facilitator_service.settle_calls[0][1] is None
    assert facilitator_service.verify_calls[0][0]
    assert facilitator_service.settle_calls[0][0] == facilitator_service.verify_calls[0][0]

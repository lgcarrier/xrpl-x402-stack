from __future__ import annotations

from contextlib import contextmanager
import socket
import threading
import time
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter as SlowLimiter
import uvicorn

import app.factory as factory_module
from app.config import Settings
from app.factory import create_app
from app.models import AssetDescriptor, SettleResponse, StructuredAmount, VerifyResponse
from xrpl_x402_middleware.middleware import PAYMENT_RESPONSE_HEADER, PaymentMiddlewareASGI, require_payment
from xrpl_x402_middleware.types import PaymentPayload, PaymentResponse
from xrpl_x402_middleware.utils import decode_model_from_base64, encode_model_to_base64

FACILITATOR_TOKEN = "local-facilitator-token"
DESTINATION = "rLOCALDESTINATION123456789"
PAYER = "rLOCALPAYER123456789"
SIGNED_TX_BLOB = "signed-local-blob"
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


@contextmanager
def _run_local_facilitator(app: FastAPI) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    host, port = sock.getsockname()

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()

    deadline = time.time() + 5
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("Local facilitator server exited before startup")
        if time.time() >= deadline:
            raise RuntimeError("Timed out waiting for local facilitator startup")
        time.sleep(0.01)

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Local facilitator server did not shut down cleanly")


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

    with _run_local_facilitator(facilitator_app) as facilitator_url:
        middleware_app = FastAPI()

        @middleware_app.get("/paid")
        async def paid(request: Request) -> dict[str, str]:
            payment = request.state.x402_payment
            return {
                "invoice_id": payment.invoice_id,
                "tx_hash": payment.tx_hash,
                "payer": payment.payer,
            }

        middleware_app.add_middleware(
            PaymentMiddlewareASGI,
            route_configs={
                "GET /paid": require_payment(
                    facilitator_url=facilitator_url,
                    bearer_token=FACILITATOR_TOKEN,
                    pay_to=DESTINATION,
                    network="xrpl:1",
                    xrp_drops=1000,
                    description="Local facilitator integration route",
                )
            },
        )

        payment_payload = PaymentPayload(
            network="xrpl:1",
            payload={
                "signedTxBlob": SIGNED_TX_BLOB,
                "invoiceId": INVOICE_ID,
            },
        )

        with TestClient(middleware_app) as client:
            response = client.get(
                "/paid",
                headers={"PAYMENT-SIGNATURE": encode_model_to_base64(payment_payload)},
            )

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
    assert facilitator_service.verify_calls == [(SIGNED_TX_BLOB, INVOICE_ID)]
    assert facilitator_service.settle_calls == [(SIGNED_TX_BLOB, INVOICE_ID)]

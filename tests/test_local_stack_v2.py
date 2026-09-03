from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    CreateHeadersAuthProvider,
    FacilitatorConfig,
    decode_payment_required_header,
    decode_payment_response_header,
    decode_payment_signature_header,
)
from x402.schemas import PaymentPayload, PaymentRequirements, SettleResponse, VerifyResponse
from xrpl.wallet import Wallet

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_client import (
    XRPLAssetSpendLimit,
    XRPLPaymentSigner,
    wrap_httpx_with_xrpl_payment,
)
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.factory import create_app
from xrpl_x402_middleware import (
    PaymentMiddlewareASGI,
    XRPLFacilitatorClient,
    XRPLPaymentContext,
    require_payment,
)

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
FACILITATOR_TOKEN = "local-v2-facilitator-token"
NETWORK = "xrpl:1"
TRANSACTION = "C" * 64


class AsyncRedis:
    """Small Redis protocol double for facilitator HTTP bookkeeping."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def aclose(self) -> None:
        return None

    async def hsetnx(self, key: str, field: str, value: str) -> int:
        record = self.hashes.setdefault(key, {})
        if field in record:
            return 0
        record[field] = value
        return 1

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: str) -> int:
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        del key, ttl
        return True


class RecordingXRPLMechanism:
    """Ledger boundary for an otherwise real local HTTP stack."""

    scheme = "exact"
    caip_family = "xrpl:*"

    def __init__(self, payer: str) -> None:
        self.payer = payer
        self.verify_calls: list[tuple[PaymentPayload, PaymentRequirements]] = []
        self.settle_calls: list[tuple[PaymentPayload, PaymentRequirements]] = []

    @staticmethod
    def get_extra(network: str) -> dict[str, Any]:
        del network
        return {
            "areFeesSponsored": False,
            "defaultAssetTransferMethod": "sequence",
            "assetTransferMethods": ["sequence", "ticketSequence"],
            "paymentFlows": {
                "sequence": {
                    "supported": ["authorization"],
                    "default": "authorization",
                },
                "ticketSequence": {
                    "supported": ["authorization"],
                    "default": "authorization",
                },
            },
        }

    @staticmethod
    def get_signers(network: str) -> list[str]:
        del network
        return []

    def verify(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        context: Any = None,
    ) -> VerifyResponse:
        del context
        self.verify_calls.append((payload, requirements))
        return VerifyResponse(
            is_valid=True,
            payer=self.payer,
            extra={"verificationPath": "local-test"},
        )

    def settle(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        context: Any = None,
    ) -> SettleResponse:
        del context
        self.settle_calls.append((payload, requirements))
        return SettleResponse(
            success=True,
            payer=self.payer,
            transaction=TRANSACTION,
            network=str(requirements.network),
            amount=requirements.amount,
        )


class InProcessFacilitatorClient(XRPLFacilitatorClient):
    """Use the facilitator ASGI app for the SDK's synchronous discovery call."""

    def __init__(self, app: FastAPI, config: FacilitatorConfig) -> None:
        self._facilitator_app = app
        super().__init__(config, pending_attempts=0)

    def _get_sync_client(self) -> TestClient:
        return TestClient(
            self._facilitator_app,
            base_url="http://facilitator.local",
        )


class HeaderProbe:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.exchanges: list[tuple[dict[str, str], int, dict[str, str]]] = []

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        request_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

        async def capture(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
                self.exchanges.append(
                    (request_headers, int(message["status"]), response_headers)
                )
            await send(message)

        await self.app(scope, receive, capture)


def test_canonical_v2_local_client_merchant_facilitator_round_trip(
    monkeypatch,
) -> None:
    asyncio.run(_exercise_local_round_trip(monkeypatch))


async def _exercise_local_round_trip(monkeypatch) -> None:
    redis = AsyncRedis()
    monkeypatch.setattr(
        factory_module, "create_async_redis_client", lambda _: redis
    )
    monkeypatch.setattr(
        factory_module,
        "build_rate_limiter",
        lambda _: Limiter(key_func=factory_module.get_remote_address),
    )

    wallet = Wallet.create()
    mechanism = RecordingXRPLMechanism(wallet.classic_address)
    facilitator_app = create_app(
        app_settings=Settings(
            _env_file=None,
            MY_DESTINATION_ADDRESS=DESTINATION,
            FACILITATOR_BEARER_TOKEN=FACILITATOR_TOKEN,
            REDIS_URL="redis://unused:6379/0",
            NETWORK_ID=NETWORK,
            ENABLE_BAZAAR=False,
        ),
        xrpl_service=mechanism,
    )
    facilitator_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url="http://facilitator.local",
    )
    facilitator_client = InProcessFacilitatorClient(
        facilitator_app,
        FacilitatorConfig(
            url="http://facilitator.local",
            http_client=facilitator_http,
            auth_provider=CreateHeadersAuthProvider(
                lambda: {
                    "verify": {
                        "Authorization": f"Bearer {FACILITATOR_TOKEN}"
                    },
                    "settle": {
                        "Authorization": f"Bearer {FACILITATOR_TOKEN}"
                    },
                }
            ),
        ),
    )

    merchant_app = FastAPI()
    handled_requests: list[Request] = []
    handler_calls = 0

    @merchant_app.get("/paid")
    async def paid(request: Request) -> dict[str, bool]:
        nonlocal handler_calls
        handler_calls += 1
        handled_requests.append(request)
        assert not hasattr(request.state, "x402_payment")
        return {"protected": True}

    merchant_app.add_middleware(
        PaymentMiddlewareASGI,
        routes={
            "GET /paid": require_payment(
                pay_to=DESTINATION,
                network=NETWORK,
                xrp_drops=1000,
                resource="http://merchant.local/paid",
                description="Local canonical v2 integration",
                service_name="Local merchant",
            )
        },
        facilitator_client=facilitator_client,
        enable_bazaar=False,
    )
    probe = HeaderProbe(merchant_app)
    signer = XRPLPaymentSigner(
        wallet,
        network=NETWORK,
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=99_999_999,
    )

    try:
        async with wrap_httpx_with_xrpl_payment(
            signer,
            asset_limits=[
                XRPLAssetSpendLimit(
                    network=NETWORK,
                    asset="XRP",
                    max_amount="1000",
                )
            ],
            transport=httpx.ASGITransport(app=probe),
            base_url="http://merchant.local",
        ) as client:
            response = await client.get("/paid")
    finally:
        await facilitator_http.aclose()

    assert response.status_code == 200
    assert response.json() == {"protected": True}
    assert handler_calls == 1
    assert len(mechanism.verify_calls) == 1
    assert len(mechanism.settle_calls) == 1

    required_name = PAYMENT_REQUIRED_HEADER.lower()
    signature_name = PAYMENT_SIGNATURE_HEADER.lower()
    response_name = PAYMENT_RESPONSE_HEADER.lower()
    assert len(probe.exchanges) == 2
    initial_request, initial_status, initial_response = probe.exchanges[0]
    paid_request, paid_status, paid_response = probe.exchanges[1]
    assert initial_status == 402
    assert signature_name not in initial_request
    assert required_name in initial_response
    assert paid_status == 200
    assert signature_name in paid_request
    assert response_name in paid_response
    assert "x-payment" not in paid_request
    assert "x-payment-response" not in paid_response

    challenge = decode_payment_required_header(initial_response[required_name])
    signed_payload = decode_payment_signature_header(paid_request[signature_name])
    settlement = decode_payment_response_header(paid_response[response_name])
    assert challenge.x402_version == 2
    assert signed_payload.x402_version == 2
    assert signed_payload.accepted == challenge.accepts[0]
    assert settlement.success is True
    assert settlement.transaction == TRANSACTION

    verify_payload, verify_requirements = mechanism.verify_calls[0]
    settle_payload, settle_requirements = mechanism.settle_calls[0]
    assert verify_payload == signed_payload
    assert settle_payload == verify_payload
    assert verify_requirements == signed_payload.accepted
    assert settle_requirements == verify_requirements
    assert verify_payload.payload["signedTxBlob"]

    payment_context = handled_requests[0].state.x402_payment
    assert isinstance(payment_context, XRPLPaymentContext)
    assert payment_context.settlement == settlement
    assert payment_context.accepted.amount == "1000"
    assert payment_context.accepted.network == NETWORK
    assert payment_context.accepted.pay_to == DESTINATION

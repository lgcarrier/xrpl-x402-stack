from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from slowapi import Limiter as SlowLimiter
from xrpl.core import binarycodec
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.wallet import Wallet

import app.factory as factory_module
from app.assets import (
    RLUSD_HEX,
    RLUSD_TESTNET_ISSUER,
    TF_PARTIAL_PAYMENT,
    USDC_HEX,
    USDC_TESTNET_ISSUER,
)
from app.config import Settings
from app.factory import create_app
from app.gateway_auth import RedisGatewayAuthenticator, hash_gateway_token
from app.models import (
    INVOICE_ID_MAX_LENGTH,
    SIGNED_TX_BLOB_MAX_LENGTH,
    AssetDescriptor,
    StructuredAmount,
    VerifyResponse,
)
from app.xrpl_service import XRPLService
from tests.fakes import FakeRedis

TEST_DESTINATION = "rTESTDESTINATIONADDRESS123456789"
TEST_ACCOUNT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
TEST_VALID_DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
MATCHING_INVOICE_ID = "A" * 64
DEFAULT_BEARER_TOKEN = "test-facilitator-token"
REDIS_GATEWAY_TOKEN = "test-redis-gateway-token"


@dataclass
class FakePayment:
    destination: str
    amount: object
    account: str = TEST_ACCOUNT
    invoice_id: str | None = "INVOICE-123"
    flags: int = 0
    last_ledger_sequence: int | None = None
    tx_hash: str = "ABC123"

    def get_hash(self) -> str:
        return self.tx_hash


def build_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "MY_DESTINATION_ADDRESS": TEST_DESTINATION,
            "NETWORK_ID": "xrpl:1",
            "FACILITATOR_BEARER_TOKEN": DEFAULT_BEARER_TOKEN,
            "REDIS_URL": "redis://fake:6379/0",
            **overrides,
        },
    )


def build_client(
    xrpl_service: XRPLService | None = None,
    gateway_authenticator: object | None = None,
    authorization_token: str | None = DEFAULT_BEARER_TOKEN,
    redis_client: FakeRedis | None = None,
    **settings_overrides: object,
) -> TestClient:
    active_redis = redis_client or FakeRedis()
    settings = build_settings(**settings_overrides)
    service = xrpl_service or XRPLService(settings, redis_client=active_redis)
    client = TestClient(
        create_app_with_in_memory_rate_limiter(
            app_settings=settings,
            xrpl_service=service,
            gateway_authenticator=gateway_authenticator,
        )
    )
    if authorization_token is not None:
        client.headers.update({"Authorization": f"Bearer {authorization_token}"})
    return client


def build_service(**settings_overrides: object) -> XRPLService:
    return XRPLService(
        build_settings(**settings_overrides),
        redis_client=FakeRedis(),
    )


def create_app_with_in_memory_rate_limiter(
    *,
    app_settings: Settings,
    xrpl_service: XRPLService | None = None,
    gateway_authenticator: object | None = None,
):
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return create_app(
            app_settings=app_settings,
            xrpl_service=xrpl_service,
            gateway_authenticator=gateway_authenticator,
        )
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


def build_public_client(
    redis_client: FakeRedis,
    xrpl_service: XRPLService | None = None,
    authorization_token: str | None = REDIS_GATEWAY_TOKEN,
    **settings_overrides: object,
) -> TestClient:
    settings = build_settings(
        GATEWAY_AUTH_MODE="redis_gateways",
        REDIS_URL="redis://fake:6379/0",
        FACILITATOR_BEARER_TOKEN=None,
        **settings_overrides,
    )
    service = xrpl_service or XRPLService(settings, redis_client=redis_client)
    client = TestClient(
        create_app_with_in_memory_rate_limiter(
            app_settings=settings,
            xrpl_service=service,
            gateway_authenticator=RedisGatewayAuthenticator(redis_client),
        )
    )
    if authorization_token is not None:
        client.headers.update({"Authorization": f"Bearer {authorization_token}"})
    return client


async def _unexpected_client_request(_request: object) -> object:
    raise AssertionError("XRPL RPC should not be called for pre-submit validation failures")


def set_client_responses(
    service: XRPLService,
    monkeypatch: pytest.MonkeyPatch,
    *results: dict[str, object],
) -> None:
    responses = [SimpleNamespace(result=result) for result in results]

    async def _client_request(_request: object) -> object:
        if not responses:
            raise AssertionError("Unexpected XRPL RPC call")
        return responses.pop(0)

    monkeypatch.setattr(service, "_client_request", _client_request)


def set_validated_ledger_sequence(
    service: XRPLService,
    monkeypatch: pytest.MonkeyPatch,
    ledger_index: int,
) -> None:
    async def _get_latest_validated_ledger_sequence() -> int:
        return ledger_index

    monkeypatch.setattr(
        service,
        "_get_latest_validated_ledger_sequence",
        _get_latest_validated_ledger_sequence,
    )


def build_unsigned_payment_blob(
    *,
    destination: str = TEST_DESTINATION,
    amount: str = "2000000",
) -> str:
    payment = Payment(
        account=TEST_ACCOUNT,
        destination=destination,
        amount=amount,
        fee="12",
        sequence=1,
    )
    return payment.blob()


def build_signed_payment_blob(
    *,
    destination: str = TEST_VALID_DESTINATION,
    amount: object = "2000000",
    invoice_id: str | None = None,
    last_ledger_sequence: int | None = None,
) -> tuple[str, str]:
    wallet = Wallet.create()
    payment_kwargs: dict[str, object] = {
        "account": wallet.classic_address,
        "destination": destination,
        "amount": amount,
        "fee": "12",
        "sequence": 1,
    }
    if invoice_id is not None:
        payment_kwargs["invoice_id"] = invoice_id
    if last_ledger_sequence is not None:
        payment_kwargs["last_ledger_sequence"] = last_ledger_sequence

    signed_payment = sign(Payment(**payment_kwargs), wallet)
    return signed_payment.blob(), signed_payment.get_hash()


def tamper_txn_signature(signed_blob: str) -> str:
    tx_dict = binarycodec.decode(signed_blob)
    signature = tx_dict["TxnSignature"]
    tx_dict["TxnSignature"] = ("0" if signature[0] != "0" else "1") + signature[1:]
    return binarycodec.encode(tx_dict)


def build_multisigned_payment_blob(
    *,
    destination: str = TEST_VALID_DESTINATION,
    amount: object = "2000000",
) -> str:
    wallet = Wallet.create()
    payment = Payment(
        account=wallet.classic_address,
        destination=destination,
        amount=amount,
        fee="12",
        sequence=1,
    )
    return sign(payment, wallet, multisign=True).blob()


def test_health_reports_network() -> None:
    client = build_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "network": "xrpl:1"}


def test_supported_reports_structured_assets() -> None:
    client = build_client()

    response = client.get("/supported")

    assert response.status_code == 200
    assert response.json() == {
        "network": "xrpl:1",
        "assets": [
            {"code": "XRP", "issuer": None},
            {"code": "RLUSD", "issuer": RLUSD_TESTNET_ISSUER},
            {"code": "USDC", "issuer": USDC_TESTNET_ISSUER},
        ],
        "settlement_mode": "validated",
    }


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_disabled_by_default(path: str) -> None:
    client = build_client()

    response = client.get(path)

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_can_be_enabled(path: str) -> None:
    client = build_client(ENABLE_API_DOCS=True)

    response = client.get(path)

    assert response.status_code == 200


def test_build_rate_limiter_uses_redis_storage_when_redis_url_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_kwargs: dict[str, object] = {}

    class RecordingLimiter:
        def __init__(self, **kwargs: object) -> None:
            recorded_kwargs.update(kwargs)
            self._storage = SimpleNamespace(check=lambda: True)

    monkeypatch.setattr(factory_module, "Limiter", RecordingLimiter)

    limiter = factory_module.build_rate_limiter(
        build_settings(REDIS_URL="redis://redis:6379/0")
    )

    assert isinstance(limiter, RecordingLimiter)
    assert recorded_kwargs == {
        "key_func": factory_module.get_remote_address,
        "storage_uri": "redis://redis:6379/0",
        "key_prefix": factory_module.RATE_LIMIT_STORAGE_KEY_PREFIX,
    }


def test_create_app_rejects_unhealthy_redis_backed_rate_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyLimiter:
        def __init__(self, **_kwargs: object) -> None:
            self._storage = SimpleNamespace(check=lambda: False)

    monkeypatch.setattr(factory_module, "Limiter", UnhealthyLimiter)

    with pytest.raises(RuntimeError, match="rate limiter storage is unavailable"):
        create_app(
            app_settings=build_settings(REDIS_URL="redis://redis:6379/0"),
        )


def test_public_routes_remain_accessible_without_bearer_auth() -> None:
    client = build_client(authorization_token=None)

    health_response = client.get("/health")
    supported_response = client.get("/supported")

    assert health_response.status_code == 200
    assert supported_response.status_code == 200


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_require_bearer_auth(endpoint: str) -> None:
    client = build_client(authorization_token=None)

    response = client.post(endpoint, json={"signed_tx_blob": "blob"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_wrong_single_token(endpoint: str) -> None:
    client = build_client(authorization_token="wrong-token")

    response = client.post(endpoint, json={"signed_tx_blob": "blob"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials"}


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_unknown_redis_gateway_token(endpoint: str) -> None:
    client = build_public_client(FakeRedis(), authorization_token="unknown-token")

    response = client.post(endpoint, json={"signed_tx_blob": "blob"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials"}


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_revoked_redis_gateway_token(endpoint: str) -> None:
    redis_client = FakeRedis()
    redis_client.seed_gateway_token(
        hash_gateway_token(REDIS_GATEWAY_TOKEN),
        gateway_id="gateway-a",
        status="revoked",
    )
    client = build_public_client(redis_client)

    response = client.post(endpoint, json={"signed_tx_blob": "blob"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials"}


def test_verify_accepts_active_redis_gateway_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    redis_client.seed_gateway_token(
        hash_gateway_token(REDIS_GATEWAY_TOKEN),
        gateway_id="gateway-a",
    )
    settings = build_settings(
        GATEWAY_AUTH_MODE="redis_gateways",
        REDIS_URL="redis://fake:6379/0",
        FACILITATOR_BEARER_TOKEN=None,
        MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION,
    )
    service = XRPLService(settings, redis_client=redis_client)
    set_validated_ledger_sequence(service, monkeypatch, 100)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        last_ledger_sequence=110,
    )

    client = build_public_client(
        redis_client,
        service,
        MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION,
    )
    response = client.post("/verify", json={"signed_tx_blob": signed_blob})

    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_settle_accepts_active_redis_gateway_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    redis_client.seed_gateway_token(
        hash_gateway_token(REDIS_GATEWAY_TOKEN),
        gateway_id="gateway-a",
    )
    settings = build_settings(
        GATEWAY_AUTH_MODE="redis_gateways",
        REDIS_URL="redis://fake:6379/0",
        FACILITATOR_BEARER_TOKEN=None,
        SETTLEMENT_MODE="optimistic",
        MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION,
    )
    service = XRPLService(settings, redis_client=redis_client)
    set_validated_ledger_sequence(service, monkeypatch, 100)
    signed_blob, tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        last_ledger_sequence=110,
    )
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    client = build_public_client(
        redis_client,
        service,
        MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION,
        SETTLEMENT_MODE="optimistic",
    )
    response = client.post("/settle", json={"signed_tx_blob": signed_blob})

    assert response.status_code == 200
    assert response.json() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "submitted",
    }


def test_verify_rate_limit_is_scoped_to_gateway_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    token_a = "gateway-a-token"
    token_b = "gateway-b-token"
    redis_client.seed_gateway_token(
        hash_gateway_token(token_a),
        gateway_id="gateway-a",
    )
    redis_client.seed_gateway_token(
        hash_gateway_token(token_b),
        gateway_id="gateway-b",
    )
    settings = build_settings(
        GATEWAY_AUTH_MODE="redis_gateways",
        REDIS_URL="redis://fake:6379/0",
        FACILITATOR_BEARER_TOKEN=None,
    )
    service = XRPLService(settings, redis_client=redis_client)

    async def _verify_payment(
        _signed_tx_blob: str,
        _invoice_id: str | None = None,
    ) -> VerifyResponse:
        return VerifyResponse(
            valid=True,
            invoice_id="INVOICE-123",
            amount="2 XRP",
            asset=AssetDescriptor(code="XRP", issuer=None),
            amount_details=StructuredAmount(
                value="2000000",
                unit="drops",
                asset=AssetDescriptor(code="XRP", issuer=None),
                drops=2000000,
            ),
            payer=TEST_ACCOUNT,
            destination=TEST_DESTINATION,
        )

    monkeypatch.setattr(service, "verify_payment", _verify_payment)
    client = TestClient(
        create_app_with_in_memory_rate_limiter(
            app_settings=settings,
            xrpl_service=service,
            gateway_authenticator=RedisGatewayAuthenticator(redis_client),
        )
    )
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    for _ in range(30):
        response = client.post(
            "/verify",
            json={"signed_tx_blob": "blob"},
            headers=headers_a,
        )
        assert response.status_code == 200

    response = client.post(
        "/verify",
        json={"signed_tx_blob": "blob"},
        headers=headers_a,
    )
    assert response.status_code == 429

    response = client.post(
        "/verify",
        json={"signed_tx_blob": "blob"},
        headers=headers_b,
    )
    assert response.status_code == 200


def test_verify_invalid_token_requests_are_rate_limited() -> None:
    client = build_client(authorization_token="wrong-token")

    for _ in range(30):
        response = client.post("/verify", json={"signed_tx_blob": "blob"})
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid authentication credentials"}

    response = client.post("/verify", json={"signed_tx_blob": "blob"})

    assert response.status_code == 429
    assert response.json()["error"].startswith("Rate limit exceeded:")


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_public_mode_requires_last_ledger_sequence(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    redis_client.seed_gateway_token(
        hash_gateway_token(REDIS_GATEWAY_TOKEN),
        gateway_id="gateway-a",
    )
    settings = build_settings(
        GATEWAY_AUTH_MODE="redis_gateways",
        REDIS_URL="redis://fake:6379/0",
        FACILITATOR_BEARER_TOKEN=None,
        MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION,
    )
    service = XRPLService(settings, redis_client=redis_client)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)
    signed_blob, _tx_hash = build_signed_payment_blob(destination=TEST_VALID_DESTINATION)

    client = build_public_client(
        redis_client,
        service,
        MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION,
    )
    response = client.post(endpoint, json={"signed_tx_blob": signed_blob})

    assert response.status_code == 402
    assert "LastLedgerSequence required" in response.json()["detail"]


def test_verify_requires_signed_blob() -> None:
    client = build_client()

    response = client.post("/verify", json={})

    assert response.status_code == 400
    assert response.json() == {"detail": "signed_tx_blob required"}


def test_settle_requires_signed_blob() -> None:
    client = build_client()

    response = client.post("/settle", json={})

    assert response.status_code == 400
    assert response.json() == {"detail": "signed_tx_blob required"}


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_unsigned_payment_blobs(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)
    client = build_client(service)

    response = client.post(
        endpoint,
        json={
            "signed_tx_blob": build_unsigned_payment_blob(
                destination=TEST_VALID_DESTINATION,
            )
        },
    )

    assert response.status_code == 402
    assert "Transaction must be signed" in response.json()["detail"]


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_tampered_signatures(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)
    client = build_client(service)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
    )

    response = client.post(
        endpoint,
        json={"signed_tx_blob": tamper_txn_signature(signed_blob)},
    )

    assert response.status_code == 402
    assert "Transaction signature invalid" in response.json()["detail"]


def test_verify_rejects_multisigned_payment_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)
    client = build_client(service)

    response = client.post(
        "/verify",
        json={
            "signed_tx_blob": build_multisigned_payment_blob(
                destination=TEST_VALID_DESTINATION,
            )
        },
    )

    assert response.status_code == 402
    assert "Multisigned transactions are not supported" in response.json()["detail"]


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("signed_tx_blob", "a" * (SIGNED_TX_BLOB_MAX_LENGTH + 1)),
        ("invoice_id", "i" * (INVOICE_ID_MAX_LENGTH + 1)),
    ],
)
def test_payment_request_field_length_limits(
    field_name: str,
    field_value: str,
) -> None:
    client = build_client()

    response = client.post(
        "/verify",
        json={"signed_tx_blob": "blob", field_name: field_value},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_oversized_request_bodies(endpoint: str) -> None:
    client = build_client(MAX_REQUEST_BODY_BYTES=64)
    body = json.dumps({"signed_tx_blob": "a" * 100})

    response = client.post(
        endpoint,
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


def test_verify_returns_issuer_aware_asset_metadata() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "1.25",
        },
    )
    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post("/verify", json={"signed_tx_blob": signed_blob})

    assert response.status_code == 200
    payer = binarycodec.decode(signed_blob)["Account"]
    assert response.json() == {
        "valid": True,
        "invoice_id": hashlib.sha256(signed_blob.encode("utf-8")).hexdigest()[:32],
        "amount": "1.25 RLUSD",
        "asset": {"code": "RLUSD", "issuer": RLUSD_TESTNET_ISSUER},
        "amount_details": {
            "value": "1.25",
            "unit": "issued",
            "asset": {"code": "RLUSD", "issuer": RLUSD_TESTNET_ISSUER},
            "drops": None,
        },
        "payer": payer,
        "destination": TEST_VALID_DESTINATION,
        "message": "Payment valid",
    }


def test_verify_returns_builtin_usdc_asset_metadata() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": USDC_HEX,
            "issuer": USDC_TESTNET_ISSUER,
            "value": "2.5",
        },
    )
    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post("/verify", json={"signed_tx_blob": signed_blob})

    assert response.status_code == 200
    payer = binarycodec.decode(signed_blob)["Account"]
    assert response.json() == {
        "valid": True,
        "invoice_id": hashlib.sha256(signed_blob.encode("utf-8")).hexdigest()[:32],
        "amount": "2.5 USDC",
        "asset": {"code": "USDC", "issuer": USDC_TESTNET_ISSUER},
        "amount_details": {
            "value": "2.5",
            "unit": "issued",
            "asset": {"code": "USDC", "issuer": USDC_TESTNET_ISSUER},
            "drops": None,
        },
        "payer": payer,
        "destination": TEST_VALID_DESTINATION,
        "message": "Payment valid",
    }


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_zero_value_issued_assets(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "0",
        },
    )
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)

    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post(endpoint, json={"signed_tx_blob": signed_blob})

    assert response.status_code == 402
    assert "greater than zero" in response.json()["detail"]


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_unbound_request_invoice_id(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)
    signed_blob, _tx_hash = build_signed_payment_blob(destination=TEST_VALID_DESTINATION)

    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post(
        endpoint,
        json={
            "signed_tx_blob": signed_blob,
            "invoice_id": MATCHING_INVOICE_ID,
        },
    )

    assert response.status_code == 402
    assert "requires transaction InvoiceID" in response.json()["detail"]


def test_verify_accepts_matching_request_invoice_id() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        invoice_id=MATCHING_INVOICE_ID,
    )

    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post(
        "/verify",
        json={
            "signed_tx_blob": signed_blob,
            "invoice_id": MATCHING_INVOICE_ID,
        },
    )

    assert response.status_code == 200
    assert response.json()["invoice_id"] == MATCHING_INVOICE_ID


def test_settle_accepts_matching_request_invoice_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        invoice_id=MATCHING_INVOICE_ID,
    )
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {"validated": True, "meta": {"delivered_amount": "2000000"}},
    )

    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post(
        "/settle",
        json={
            "signed_tx_blob": signed_blob,
            "invoice_id": MATCHING_INVOICE_ID,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "validated",
    }


def test_settle_rejects_wrong_destination_without_prior_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_DESTINATION)
    payment = FakePayment(destination="rWRONGDESTINATION123456789", amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)

    client = build_client(service)
    response = client.post("/settle", json={"signed_tx_blob": "blob"})

    assert response.status_code == 402
    assert "Wrong destination address" in response.json()["detail"]


def test_settle_rejects_below_minimum_without_prior_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(
        MY_DESTINATION_ADDRESS=TEST_DESTINATION,
        MIN_XRP_DROPS=2000,
    )
    payment = FakePayment(destination=TEST_DESTINATION, amount="1000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)

    client = build_client(service)
    response = client.post("/settle", json={"signed_tx_blob": "blob"})

    assert response.status_code == 402
    assert "Payment below minimum amount" in response.json()["detail"]


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_routes_reject_unsupported_issued_asset(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": TEST_ACCOUNT,
            "value": "5",
        },
    )
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)

    client = build_client(service, MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    response = client.post(endpoint, json={"signed_tx_blob": signed_blob})

    assert response.status_code == 402
    assert "Unsupported issued asset" in response.json()["detail"]


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_routes_reject_partial_payments(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_DESTINATION)
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        flags=TF_PARTIAL_PAYMENT,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    monkeypatch.setattr(service, "_client_request", _unexpected_client_request)

    client = build_client(service)
    response = client.post(endpoint, json={"signed_tx_blob": "blob"})

    assert response.status_code == 402
    assert "Partial payments are not supported" in response.json()["detail"]

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slowapi import Limiter
from x402.schemas import SettleResponse, VerifyResponse

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.factory import create_app
from xrpl_x402_facilitator.gateway_auth import hash_gateway_token

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
SINGLE_TOKEN = "single-token"


class AsyncRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def aclose(self) -> None:
        return None

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def seed_gateway(
        self,
        token: str,
        *,
        gateway_id: str,
        status: str = "active",
    ) -> None:
        self.hashes[
            f"facilitator:gateway_token:{hash_gateway_token(token)}"
        ] = {
            "gateway_id": gateway_id,
            "status": status,
        }


class StubMechanism:
    scheme = "exact"
    caip_family = "xrpl:*"

    def get_extra(self, _network: str) -> dict[str, Any]:
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

    def get_signers(self, _network: str) -> list[str]:
        return []

    def verify(
        self,
        _payload: object,
        _requirements: object,
        context: object | None = None,
    ) -> VerifyResponse:
        del context
        return VerifyResponse(
            is_valid=False,
            invalid_reason="invalid_test_payment",
            invalid_message="not authorized",
        )

    def settle(
        self,
        _payload: object,
        requirements: object,
        context: object | None = None,
    ) -> SettleResponse:
        del context
        return SettleResponse(
            success=False,
            error_reason="invalid_test_payment",
            transaction="",
            network=str(getattr(requirements, "network")),
        )


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "MY_DESTINATION_ADDRESS": DESTINATION,
        "FACILITATOR_BEARER_TOKEN": SINGLE_TOKEN,
        "REDIS_URL": "redis://unused:6379/0",
        "NETWORK_ID": "xrpl:1",
        "ENABLE_BAZAAR": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def build_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    redis: AsyncRedis | None = None,
    limiter: Limiter | None = None,
    **settings_overrides: object,
):
    active_redis = redis or AsyncRedis()
    active_limiter = limiter or Limiter(
        key_func=factory_module.get_remote_address
    )
    monkeypatch.setattr(
        factory_module,
        "create_async_redis_client",
        lambda _url: active_redis,
    )
    monkeypatch.setattr(
        factory_module,
        "build_rate_limiter",
        lambda _settings: active_limiter,
    )
    return create_app(
        app_settings=build_settings(**settings_overrides),
        xrpl_service=StubMechanism(),
    )


def canonical_envelope() -> dict[str, Any]:
    requirements = {
        "scheme": "exact",
        "network": "xrpl:1",
        "asset": "XRP",
        "amount": "1000",
        "payTo": DESTINATION,
        "maxTimeoutSeconds": 60,
        "extra": {
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    }
    return {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "payload": {"signedTxBlob": "AA"},
            "accepted": copy.deepcopy(requirements),
        },
        "paymentRequirements": requirements,
    }


def test_health_is_public_and_reports_protocol_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_test_app(monkeypatch)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "network": "xrpl:1",
        "x402Version": "2",
    }
    assert app.version == "0.2.0"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_disabled_by_default(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_test_app(monkeypatch)

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_can_be_enabled(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_test_app(monkeypatch, ENABLE_API_DOCS=True)

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    if path == "/openapi.json":
        assert response.json()["info"]["version"] == "0.2.0"


def test_redis_rate_limiter_checks_storage_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_kwargs: dict[str, object] = {}

    class HealthyLimiter:
        def __init__(self, **kwargs: object) -> None:
            recorded_kwargs.update(kwargs)
            self._storage = SimpleNamespace(check=lambda: True)

    monkeypatch.setattr(factory_module, "Limiter", HealthyLimiter)

    limiter = factory_module.build_rate_limiter(build_settings())

    assert isinstance(limiter, HealthyLimiter)
    assert recorded_kwargs == {
        "key_func": factory_module.get_remote_address,
        "storage_uri": "redis://unused:6379/0",
        "key_prefix": factory_module.RATE_LIMIT_STORAGE_KEY_PREFIX,
    }


def test_unhealthy_redis_rate_limiter_prevents_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyLimiter:
        def __init__(self, **_kwargs: object) -> None:
            self._storage = SimpleNamespace(check=lambda: False)

    monkeypatch.setattr(factory_module, "Limiter", UnhealthyLimiter)

    with pytest.raises(
        RuntimeError,
        match="Redis-backed rate limiter storage is unavailable",
    ):
        factory_module.build_rate_limiter(build_settings())


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_single_token_authentication(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_test_app(monkeypatch)
    envelope = canonical_envelope()

    with TestClient(app) as client:
        missing = client.post(endpoint, json=envelope)
        wrong = client.post(
            endpoint,
            json=envelope,
            headers={"Authorization": "Bearer wrong-token"},
        )
        accepted = client.post(
            endpoint,
            json=envelope,
            headers={"Authorization": f"Bearer {SINGLE_TOKEN}"},
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert accepted.status_code == 200


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_redis_gateway_authentication_and_revocation(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AsyncRedis()
    redis.seed_gateway("active-token", gateway_id="gateway-active")
    redis.seed_gateway(
        "revoked-token",
        gateway_id="gateway-revoked",
        status="revoked",
    )
    app = build_test_app(
        monkeypatch,
        redis=redis,
        GATEWAY_AUTH_MODE="redis_gateways",
        FACILITATOR_BEARER_TOKEN=None,
    )
    envelope = canonical_envelope()

    with TestClient(app) as client:
        unknown = client.post(
            endpoint,
            json=envelope,
            headers={"Authorization": "Bearer unknown-token"},
        )
        revoked = client.post(
            endpoint,
            json=envelope,
            headers={"Authorization": "Bearer revoked-token"},
        )
        active = client.post(
            endpoint,
            json=envelope,
            headers={"Authorization": "Bearer active-token"},
        )

    assert unknown.status_code == 401
    assert revoked.status_code == 401
    assert active.status_code == 200


def test_verify_rate_limit_is_scoped_to_gateway_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = AsyncRedis()
    redis.seed_gateway("gateway-a-token", gateway_id="gateway-a")
    redis.seed_gateway("gateway-b-token", gateway_id="gateway-b")
    app = build_test_app(
        monkeypatch,
        redis=redis,
        GATEWAY_AUTH_MODE="redis_gateways",
        FACILITATOR_BEARER_TOKEN=None,
    )
    envelope = canonical_envelope()
    gateway_a = {"Authorization": "Bearer gateway-a-token"}
    gateway_b = {"Authorization": "Bearer gateway-b-token"}

    with TestClient(app) as client:
        for _ in range(30):
            response = client.post(
                "/verify", json=envelope, headers=gateway_a
            )
            assert response.status_code == 200

        limited = client.post(
            "/verify", json=envelope, headers=gateway_a
        )
        independent = client.post(
            "/verify", json=envelope, headers=gateway_b
        )

    assert limited.status_code == 429
    assert independent.status_code == 200


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
def test_payment_routes_reject_oversized_bodies(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_test_app(monkeypatch, MAX_REQUEST_BODY_BYTES=64)
    body = json.dumps({"x402Version": 2, "padding": "a" * 100})

    with TestClient(app) as client:
        response = client.post(
            endpoint,
            content=body,
            headers={
                "Authorization": f"Bearer {SINGLE_TOKEN}",
                "content-type": "application/json",
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


@pytest.mark.parametrize("endpoint", ["/verify", "/settle"])
@pytest.mark.parametrize(
    "malformed",
    [
        {
            "x402Version": 2,
            "paymentPayload": {
                "x402Version": 2,
                "payload": {"signedTxBlob": "AA"},
            },
            "paymentRequirements": canonical_envelope()[
                "paymentRequirements"
            ],
        },
        {
            "x402Version": 2,
            "paymentPayload": canonical_envelope()["paymentPayload"],
            "paymentRequirements": {
                "scheme": "exact",
                "network": "xrpl:1",
                "asset": "XRP",
                "amount": "1000",
                "maxTimeoutSeconds": 60,
            },
        },
        {
            **canonical_envelope(),
            "x402Version": 1,
        },
        {
            **canonical_envelope(),
            "unexpected": "field",
        },
        {
            **canonical_envelope(),
            "paymentPayload": {
                **canonical_envelope()["paymentPayload"],
                "payload": {"signed_tx_blob": "AA"},
            },
        },
        {
            **canonical_envelope(),
            "paymentPayload": {
                **canonical_envelope()["paymentPayload"],
                "accepted": {
                    **canonical_envelope()["paymentPayload"]["accepted"],
                    "maxAmountRequired": {"amount": "1000"},
                },
            },
        },
        {
            **canonical_envelope(),
            "paymentRequirements": {
                **canonical_envelope()["paymentRequirements"],
                "maxAmountRequired": {"amount": "1000"},
            },
        },
        {"signed_tx_blob": "AA"},
    ],
    ids=[
        "payload-missing-accepted",
        "requirements-missing-pay-to",
        "wrong-version",
        "unexpected-field",
        "legacy-inner-payload-alias",
        "legacy-accepted-requirements-field",
        "legacy-payment-requirements-field",
        "legacy-custom-envelope",
    ],
)
def test_payment_routes_reject_malformed_v2_envelopes(
    endpoint: str,
    malformed: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_test_app(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            endpoint,
            json=malformed,
            headers={"Authorization": f"Bearer {SINGLE_TOKEN}"},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail
    serialized = json.dumps(malformed)
    for legacy_field in ("signed_tx_blob", "maxAmountRequired"):
        if legacy_field in serialized:
            assert legacy_field in str(detail)

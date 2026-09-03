from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from slowapi import Limiter
from x402.extensions.bazaar import declare_discovery_extension
from x402.extensions.bazaar.facilitator_client import _parse_list_response
from x402.extensions.payment_identifier import (
    declare_payment_identifier_extension,
)
from x402.schemas import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
)

import xrpl_x402_facilitator.factory as factory_module
from xrpl_x402_facilitator.catalog import BazaarCatalog
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.idempotency import PaymentIdentifierStore

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
TOKEN = "facilitator-test-token"


class AsyncRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, set[str]] = {}

    async def aclose(self) -> None:
        return None

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        self.strings[key] = value
        return True

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
        return True

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        self.sorted_sets.setdefault(key, set()).update(mapping)
        return len(mapping)

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        return sorted(self.sorted_sets.get(key, set()))

    async def zrem(self, key: str, *values: str) -> int:
        current = self.sorted_sets.setdefault(key, set())
        removed = sum(value in current for value in values)
        current.difference_update(values)
        return removed


def requirements(amount: str = "1000") -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount=amount,
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )


def identified_payload(
    accepted: PaymentRequirements,
    identifier: str = "pay_identifier_0000001",
) -> PaymentPayload:
    extension = declare_payment_identifier_extension(required=True)
    extension["info"]["id"] = identifier
    return PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        extensions={"payment-identifier": extension},
    )


def test_payment_identifier_caches_matching_and_conflicts_on_reuse() -> None:
    asyncio.run(_exercise_payment_identifier())


async def _exercise_payment_identifier() -> None:
    store = PaymentIdentifierStore(AsyncRedis(), ttl_seconds=300)
    accepted = requirements()
    payload = identified_payload(accepted)
    key, cached = await store.get_cached("verify", payload, accepted)
    assert key and cached is None
    await store.put(key, "verify", {"isValid": True})
    _, cached = await store.get_cached("verify", payload, accepted)
    assert cached == {"isValid": True}

    missing_identifier = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        extensions={
            "payment-identifier": declare_payment_identifier_extension(
                required=True
            )
        },
    )
    with pytest.raises(Exception) as missing_exc:
        await store.bind(missing_identifier, accepted)
    assert getattr(missing_exc.value, "status_code", None) == 400

    different = requirements(amount="2000")
    mismatched = identified_payload(different)
    with pytest.raises(Exception) as exc:
        await store.get_cached("verify", mismatched, different)
    assert getattr(exc.value, "status_code", None) == 409


def test_bazaar_catalog_is_canonical_deterministic_and_paginated() -> None:
    asyncio.run(_exercise_bazaar_catalog())


async def _exercise_bazaar_catalog() -> None:
    redis = AsyncRedis()
    catalog = BazaarCatalog(redis, retention_seconds=300)
    accepted = requirements()
    extensions = declare_discovery_extension(
        input={"query": "alpha"},
        input_schema={
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    extensions["bazaar"]["info"]["input"]["method"] = "GET"
    extensions["opaque-extension"] = {"vendorField": "preserved"}
    payload = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        resource={
            "url": "https://merchant.example/data?ignored=yes#fragment",
            "description": "Sanitized data",
            "mimeType": "application/json",
            "serviceName": "Example Merchant",
            "tags": ["alpha", "ALPHA", "data"],
            "iconUrl": "https://merchant.example/icon.png",
        },
        extensions=extensions,
    )
    assert await catalog.index(payload, accepted) is True
    result = await catalog.list(
        query="example merchant",
        pay_to=DESTINATION,
        scheme="exact",
        network="xrpl:1",
        extension="opaque-extension",
        limit=10,
        offset=0,
    )
    assert result["pagination"] == {"limit": 10, "offset": 0, "total": 1}
    item = result["items"][0]
    assert set(item) == {
        "resource",
        "type",
        "x402Version",
        "accepts",
        "lastUpdated",
        "description",
        "mimeType",
        "serviceName",
        "tags",
        "iconUrl",
        "extensions",
    }
    assert item["resource"] == "https://merchant.example/data"
    assert item["type"] == "http"
    assert item["x402Version"] == 2
    assert item["accepts"] == [
        accepted.model_dump(mode="json", by_alias=True, exclude_none=True)
    ]
    assert datetime.fromisoformat(item["lastUpdated"].replace("Z", "+00:00"))
    assert item["serviceName"] == "Example Merchant"
    assert item["tags"] == ["alpha", "data"]
    assert item["extensions"]["opaque-extension"] == {
        "vendorField": "preserved"
    }

    parsed = _parse_list_response(result)
    assert parsed.x402_version == 2
    assert parsed.pagination.total == 1
    assert parsed.items[0].resource == "https://merchant.example/data"
    assert parsed.items[0].accepts == item["accepts"]
    assert parsed.items[0].service_name == "Example Merchant"
    assert parsed.items[0].extensions == item["extensions"]
    assert parsed.items[0].to_dict() == item


class InvalidMechanism:
    scheme = "exact"
    caip_family = "xrpl:*"

    def get_extra(self, network: str) -> dict[str, Any]:
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

    def get_signers(self, network: str) -> list[str]:
        return []

    def verify(self, payload, requirements, context=None):  # type: ignore[no-untyped-def]
        return VerifyResponse(
            is_valid=False,
            invalid_reason="invalid_test_payment",
            invalid_message="not authorized",
        )

    def settle(self, payload, requirements, context=None):  # type: ignore[no-untyped-def]
        return SettleResponse(
            success=False,
            error_reason="invalid_test_payment",
            transaction="",
            network=str(requirements.network),
        )


def test_verify_rechecks_current_ledger_state_for_matching_identifier(
    monkeypatch,
) -> None:
    redis = AsyncRedis()
    monkeypatch.setattr(
        factory_module, "create_async_redis_client", lambda _: redis
    )
    monkeypatch.setattr(
        factory_module,
        "build_rate_limiter",
        lambda _: Limiter(key_func=factory_module.get_remote_address),
    )

    class ChangingMechanism(InvalidMechanism):
        def __init__(self) -> None:
            self.verify_calls = 0

        def verify(
            self, payload, requirements, context=None
        ):  # type: ignore[no-untyped-def]
            del payload, requirements, context
            self.verify_calls += 1
            return VerifyResponse(
                is_valid=self.verify_calls == 1,
                invalid_reason=(
                    None if self.verify_calls == 1 else "ledger_state_changed"
                ),
            )

    mechanism = ChangingMechanism()
    configured = Settings(
        _env_file=None,
        MY_DESTINATION_ADDRESS=DESTINATION,
        FACILITATOR_BEARER_TOKEN=TOKEN,
        REDIS_URL="redis://unused:6379/0",
        NETWORK_ID="xrpl:1",
        ENABLE_BAZAAR=False,
    )
    app = factory_module.create_app(
        app_settings=configured,
        xrpl_service=mechanism,
    )
    accepted = requirements()
    envelope = {
        "x402Version": 2,
        "paymentPayload": identified_payload(accepted).model_dump(
            by_alias=True
        ),
        "paymentRequirements": accepted.model_dump(by_alias=True),
    }

    with TestClient(app) as client:
        first = client.post(
            "/verify",
            json=envelope,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        second = client.post(
            "/verify",
            json=envelope,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert first.status_code == 200
    assert first.json()["isValid"] is True
    assert second.status_code == 200
    assert second.json()["isValid"] is False
    assert second.json()["invalidReason"] == "ledger_state_changed"
    assert mechanism.verify_calls == 2


def test_facilitator_endpoints_use_canonical_envelopes_and_statuses(monkeypatch) -> None:
    redis = AsyncRedis()
    monkeypatch.setattr(
        factory_module, "create_async_redis_client", lambda _: redis
    )
    monkeypatch.setattr(
        factory_module,
        "build_rate_limiter",
        lambda _: Limiter(key_func=factory_module.get_remote_address),
    )
    configured = Settings(
        _env_file=None,
        MY_DESTINATION_ADDRESS=DESTINATION,
        FACILITATOR_BEARER_TOKEN=TOKEN,
        REDIS_URL="redis://unused:6379/0",
        NETWORK_ID="xrpl:1",
        ENABLE_BAZAAR=False,
    )
    app = factory_module.create_app(
        app_settings=configured,
        xrpl_service=InvalidMechanism(),
    )
    accepted = requirements()
    envelope = {
        "x402Version": 2,
        "paymentPayload": PaymentPayload(
            payload={"signedTxBlob": "AA"}, accepted=accepted
        ).model_dump(by_alias=True),
        "paymentRequirements": accepted.model_dump(by_alias=True),
    }

    with TestClient(app) as client:
        supported = client.get("/supported")
        unauthorized = client.post("/verify", json=envelope)
        invalid = client.post(
            "/verify",
            json=envelope,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        malformed = client.post(
            "/verify",
            json={"signed_tx_blob": "AA"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert supported.status_code == 200
    assert supported.json()["kinds"][0]["x402Version"] == 2
    supported_extra = supported.json()["kinds"][0]["extra"]
    assert supported_extra["defaultAssetTransferMethod"] == "sequence"
    assert supported_extra["paymentFlows"]["sequence"]["default"] == (
        "authorization"
    )
    assert supported_extra["paymentFlows"]["ticketSequence"]["supported"] == [
        "authorization"
    ]
    assert unauthorized.status_code == 401
    assert invalid.status_code == 200
    assert invalid.json()["isValid"] is False
    assert malformed.status_code == 400

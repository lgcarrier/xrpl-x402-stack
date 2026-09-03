from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from x402.extensions.payment_identifier import (
    extract_and_validate_payment_identifier,
)
from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

from xrpl_x402_core import requirements_fingerprint


@dataclass(frozen=True, slots=True)
class StoredResourceResponse:
    status_code: int
    content_type: str
    body: Any
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class AuthoritativeResource:
    resource: ResourceInfo
    identity: str


class RedisResourceResponseStore:
    """Caches an unsafe handler result until its signed payment settles."""

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = 600,
        safety_margin_seconds: int = 60,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if safety_margin_seconds < 0:
            raise ValueError("safety_margin_seconds cannot be negative")
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._safety_margin_seconds = safety_margin_seconds

    @staticmethod
    def _key(identifier: str, requirements: PaymentRequirements) -> str:
        scoped = f"{requirements.network}|{requirements.pay_to}|{identifier}"
        return "resource:idempotency:" + hashlib.sha256(scoped.encode()).hexdigest()

    async def bind(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        *,
        authoritative_resource: AuthoritativeResource | None = None,
    ) -> bool:
        """Atomically bind an identifier before its protected handler executes.

        Returns ``True`` to the request that acquired the binding and ``False``
        to a matching retry while the original request is still in flight.
        """

        identifier = _identifier(payload)
        if identifier is None:
            raise ValueError("unsafe paid requests require payment-identifier")
        fingerprint = _fingerprint(payload, requirements, authoritative_resource)
        record = {"fingerprint": fingerprint, "state": "bound"}
        key = self._key(identifier, requirements)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        for _attempt in range(2):
            acquired = await self._redis.set(
                key,
                encoded,
                ex=self._ttl(requirements),
                nx=True,
            )
            if acquired:
                return True
            raw = await self._redis.get(key)
            if raw:
                _validate_fingerprint(json.loads(raw), fingerprint)
                return False
        raise RuntimeError("payment-identifier binding disappeared during reservation")

    async def get(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        *,
        authoritative_resource: AuthoritativeResource | None = None,
    ) -> StoredResourceResponse | None:
        identifier = _identifier(payload)
        if identifier is None:
            return None
        raw = await self._redis.get(self._key(identifier, requirements))
        if not raw:
            return None
        record = json.loads(raw)
        _validate_fingerprint(
            record, _fingerprint(payload, requirements, authoritative_resource)
        )
        if record.get("state") == "bound" or "statusCode" not in record:
            return None
        return StoredResourceResponse(
            status_code=int(record["statusCode"]),
            content_type=str(record["contentType"]),
            body=_decode_body(record),
            headers=dict(record.get("headers") or {}),
        )

    async def put(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        response: StoredResourceResponse,
        *,
        authoritative_resource: AuthoritativeResource | None = None,
    ) -> None:
        identifier = _identifier(payload)
        if identifier is None:
            raise ValueError("unsafe paid requests require payment-identifier")
        key = self._key(identifier, requirements)
        fingerprint = _fingerprint(payload, requirements, authoritative_resource)
        existing = await self._redis.get(key)
        if existing:
            _validate_fingerprint(json.loads(existing), fingerprint)
        encoded_body, body_encoding = _encode_body(response.body)
        record = {
            "fingerprint": fingerprint,
            "state": "response",
            "statusCode": response.status_code,
            "contentType": response.content_type,
            "body": encoded_body,
            "bodyEncoding": body_encoding,
            "headers": response.headers,
        }
        await self._redis.set(
            key,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
            ex=self._ttl(requirements),
        )

    def _ttl(self, requirements: PaymentRequirements) -> int:
        return max(
            self._ttl_seconds,
            int(requirements.max_timeout_seconds) + self._safety_margin_seconds,
        )


def _fingerprint(
    payload: PaymentPayload,
    requirements: PaymentRequirements,
    authoritative_resource: AuthoritativeResource | None,
) -> str:
    return requirements_fingerprint(
        payload,
        requirements,
        authoritative_resource=(
            authoritative_resource.resource if authoritative_resource else None
        ),
        resource_identity=(
            authoritative_resource.identity if authoritative_resource else None
        ),
    )


def _validate_fingerprint(record: dict[str, Any], expected: str) -> None:
    if record.get("fingerprint") != expected:
        raise ValueError(
            "payment-identifier was reused with different payment or resource context"
        )


def _encode_body(body: Any) -> tuple[Any, str]:
    if isinstance(body, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(body)).decode("ascii"), "base64"
    return body, "json"


def _decode_body(record: dict[str, Any]) -> Any:
    encoding = record.get("bodyEncoding")
    if encoding in {None, "json"}:
        return record.get("body")
    if encoding == "base64":
        encoded = record.get("body")
        if not isinstance(encoded, str):
            raise ValueError("cached base64 response body must be a string")
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("cached response body is not valid base64") from error
    raise ValueError(f"unsupported cached response body encoding: {encoding}")


def _identifier(payload: PaymentPayload) -> str | None:
    identifier, validation = extract_and_validate_payment_identifier(payload)
    if not validation.valid:
        raise ValueError("; ".join(validation.errors))
    return identifier


__all__ = [
    "AuthoritativeResource",
    "RedisResourceResponseStore",
    "StoredResourceResponse",
]

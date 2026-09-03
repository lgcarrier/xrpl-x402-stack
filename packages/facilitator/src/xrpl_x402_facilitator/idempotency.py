from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException
from x402.extensions.payment_identifier import (
    PAYMENT_IDENTIFIER,
    extract_and_validate_payment_identifier,
    is_payment_identifier_required,
    validate_payment_identifier_requirement,
)
from x402.schemas import PaymentPayload, PaymentRequirements

from xrpl_x402_core import requirements_fingerprint


class PaymentIdentifierStore:
    def __init__(self, redis_client: Any, *, ttl_seconds: int) -> None:
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(identifier: str, requirements: PaymentRequirements) -> str:
        scope = f"{requirements.network}|{requirements.pay_to}|{identifier}"
        return (
            "facilitator:idempotency:"
            + hashlib.sha256(scope.encode()).hexdigest()
        )

    async def bind(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> str | None:
        identifier, validation = extract_and_validate_payment_identifier(payload)
        if not validation.valid:
            raise HTTPException(
                status_code=400, detail="; ".join(validation.errors)
            )
        declaration = (payload.extensions or {}).get(PAYMENT_IDENTIFIER)
        required_validation = validate_payment_identifier_requirement(
            payload,
            is_payment_identifier_required(declaration),
        )
        if not required_validation.valid:
            raise HTTPException(
                status_code=400,
                detail="; ".join(required_validation.errors),
            )
        if identifier is None:
            return None
        key = self._key(identifier, requirements)
        fingerprint = requirements_fingerprint(payload, requirements)
        await self._redis.hsetnx(key, "fingerprint", fingerprint)
        existing = await self._redis.hget(key, "fingerprint")
        if existing != fingerprint:
            raise HTTPException(
                status_code=409,
                detail=(
                    "payment-identifier was already used with a different request"
                ),
            )
        await self._redis.expire(key, self._ttl_seconds)
        return key

    async def get_cached(
        self,
        stage: str,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> tuple[str | None, dict[str, Any] | None]:
        key = await self.bind(payload, requirements)
        if key is None:
            return None, None
        cached = await self._redis.hget(key, stage)
        return key, json.loads(cached) if cached else None

    async def put(
        self, key: str | None, stage: str, response: dict[str, Any]
    ) -> None:
        if key is None:
            return
        await self._redis.hset(
            key,
            stage,
            json.dumps(response, sort_keys=True, separators=(",", ":")),
        )
        await self._redis.expire(key, self._ttl_seconds)

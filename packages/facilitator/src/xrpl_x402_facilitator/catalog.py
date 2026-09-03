from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from x402.extensions.bazaar import DiscoveryResource, extract_discovery_info
from x402.schemas import PaymentPayload, PaymentRequirements


class BazaarCatalog:
    """Redis-backed, deterministic index of validated Bazaar discoveries."""

    def __init__(self, redis_client: Any, *, retention_seconds: int) -> None:
        self._redis = redis_client
        self._retention_seconds = retention_seconds

    @staticmethod
    def _entry_key(identity: str) -> str:
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"facilitator:bazaar:entry:{digest}"

    async def index(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> bool:
        try:
            discovered = extract_discovery_info(payload, requirements)
        except Exception:
            return False
        if discovered is None:
            return False
        url = discovered.resource_url
        method = discovered.method.upper()
        tool_name = discovered.tool_name or ""
        resource_type = "mcp" if tool_name else "http"
        identity = (
            f"{url}|tool:{tool_name}" if tool_name else f"{url}|{method}"
        )
        if not url:
            return False
        accepted = requirements.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        item = DiscoveryResource(
            resource=url,
            type=resource_type,
            x402_version=discovered.x402_version,
            accepts=[accepted],
            last_updated=(
                datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            description=discovered.description,
            mime_type=discovered.mime_type,
            service_name=discovered.service_name,
            tags=discovered.tags,
            icon_url=discovered.icon_url,
            extensions=discovered.extensions,
        ).to_dict()
        key = self._entry_key(identity)
        await self._redis.set(
            key,
            json.dumps(item, sort_keys=True, separators=(",", ":")),
            ex=self._retention_seconds,
        )
        await self._redis.zadd("facilitator:bazaar:index", {key: 0})
        return True

    async def list(
        self,
        *,
        resource_type: str | None = None,
        pay_to: str | None = None,
        scheme: str | None = None,
        network: str | None = None,
        extension: str | None = None,
        limit: int = 20,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        keys = await self._redis.zrange("facilitator:bazaar:index", 0, -1)
        items: list[dict[str, Any]] = []
        stale: list[str] = []
        needle = (query or "").strip().casefold()
        for key in keys:
            raw = await self._redis.get(key)
            if not raw:
                stale.append(key)
                continue
            item = json.loads(raw)
            if resource_type and item.get("type") != resource_type:
                continue
            accepts = item.get("accepts")
            if not isinstance(accepts, list):
                accepts = []
            if (pay_to or scheme or network) and not any(
                isinstance(accepted, dict)
                and (not pay_to or accepted.get("payTo") == pay_to)
                and (not scheme or accepted.get("scheme") == scheme)
                and (not network or accepted.get("network") == network)
                for accepted in accepts
            ):
                continue
            extensions = item.get("extensions")
            if extension and (
                not isinstance(extensions, dict)
                or extension not in extensions
            ):
                continue
            searchable = json.dumps(
                {
                    "resource": item.get("resource"),
                    "description": item.get("description"),
                    "mimeType": item.get("mimeType"),
                    "serviceName": item.get("serviceName"),
                    "tags": item.get("tags"),
                    "extensions": extensions,
                },
                sort_keys=True,
            ).casefold()
            if needle and needle not in searchable:
                continue
            items.append(item)
        if stale:
            await self._redis.zrem("facilitator:bazaar:index", *stale)
        items.sort(
            key=lambda item: (
                str(item.get("resource", "")).casefold(),
                str(item.get("type", "")).casefold(),
                json.dumps(item.get("extensions"), sort_keys=True),
            )
        )
        total = len(items)
        return {
            "x402Version": 2,
            "items": items[offset : offset + limit],
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }

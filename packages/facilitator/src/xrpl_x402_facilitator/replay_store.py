from __future__ import annotations

from threading import Lock
from typing import Any, Protocol

from xrpl_x402_core import XRPLSettlementState
from xrpl_x402_facilitator.config import Settings


_MONOTONIC_UPDATE_SCRIPT = """
local current_json = redis.call("GET", KEYS[1])
if not current_json then
  return 0
end

local current = cjson.decode(current_json)
local candidate = cjson.decode(ARGV[1])
local ranks = {pending = 0, failed = 1, validated = 2}
local current_rank = ranks[current.status]
local candidate_rank = ranks[candidate.status]
if current_rank == nil or candidate_rank == nil then
  return redis.error_reply("invalid settlement state")
end

if current.status == "pending" and candidate.status == "pending" then
  redis.call("SET", KEYS[1], ARGV[1], "XX")
  return 1
end

if candidate_rank > current_rank then
  redis.call("SET", KEYS[1], ARGV[1], "XX", "EX", ARGV[2])
  return 1
end

if current.status ~= "pending" then
  redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""


_STATE_RANK = {"pending": 0, "failed": 1, "validated": 2}


class SettlementStore(Protocol):
    def get(self, transaction: str) -> XRPLSettlementState | None: ...
    def reserve(self, state: XRPLSettlementState, ttl_seconds: int) -> bool: ...
    def update(self, state: XRPLSettlementState, ttl_seconds: int) -> None: ...


class RedisSettlementStore:
    """Cross-process transaction-hash settlement coordination."""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    @staticmethod
    def _key(transaction: str) -> str:
        return f"facilitator:settlement:{transaction.upper()}"

    def get(self, transaction: str) -> XRPLSettlementState | None:
        raw = self._redis.get(self._key(transaction))
        return (
            XRPLSettlementState.model_validate_json(raw)
            if raw is not None
            else None
        )

    def reserve(self, state: XRPLSettlementState, ttl_seconds: int) -> bool:
        options: dict[str, Any] = {"nx": True}
        if state.status != "pending":
            options["ex"] = ttl_seconds
        return bool(
            self._redis.set(
                self._key(state.transaction),
                state.model_dump_json(),
                **options,
            )
        )

    def update(self, state: XRPLSettlementState, ttl_seconds: int) -> None:
        self._redis.eval(
            _MONOTONIC_UPDATE_SCRIPT,
            1,
            self._key(state.transaction),
            state.model_dump_json(),
            ttl_seconds,
        )


class InMemorySettlementStore:
    """Deterministic test store with the atomic reserve contract."""

    def __init__(self) -> None:
        self._values: dict[str, XRPLSettlementState] = {}
        self._lock = Lock()

    def get(self, transaction: str) -> XRPLSettlementState | None:
        with self._lock:
            return self._values.get(transaction.upper())

    def reserve(self, state: XRPLSettlementState, ttl_seconds: int) -> bool:
        del ttl_seconds
        with self._lock:
            key = state.transaction.upper()
            if key in self._values:
                return False
            self._values[key] = state
            return True

    def update(self, state: XRPLSettlementState, ttl_seconds: int) -> None:
        del ttl_seconds
        with self._lock:
            key = state.transaction.upper()
            current = self._values.get(key)
            if current is None:
                return
            current_rank = _STATE_RANK[current.status]
            candidate_rank = _STATE_RANK[state.status]
            if (
                current.status == "pending"
                and state.status == "pending"
            ) or candidate_rank > current_rank:
                self._values[key] = state


def create_sync_redis_client(url: str) -> Any:
    import redis

    return redis.from_url(url, decode_responses=True)


def build_settlement_store(
    settings: Settings, redis_client: Any | None = None
) -> SettlementStore:
    client = redis_client or create_sync_redis_client(
        settings.REDIS_URL.get_secret_value()
    )
    return RedisSettlementStore(client)

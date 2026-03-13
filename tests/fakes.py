from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class FakeWatchError(Exception):
    pass


@dataclass
class _StringRecord:
    value: str
    expires_at: float | None = None


@dataclass
class _HashRecord:
    value: dict[str, str]
    expires_at: float | None = None


class FakeRedis:
    WatchError = FakeWatchError

    def __init__(self) -> None:
        self._now = 0.0
        self._strings: dict[str, _StringRecord] = {}
        self._hashes: dict[str, _HashRecord] = {}

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._purge_expired()

    async def aclose(self) -> None:
        return None

    async def mget(self, *keys: str) -> list[str | None]:
        self._purge_expired()
        return [self._get_string_value(key) for key in keys]

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._purge_expired()
        self._strings[key] = _StringRecord(
            value=value,
            expires_at=self._expires_at(ex),
        )
        return True

    async def delete(self, key: str) -> int:
        self._purge_expired()
        removed = 0
        if self._strings.pop(key, None) is not None:
            removed += 1
        if self._hashes.pop(key, None) is not None:
            removed += 1
        return removed

    async def hgetall(self, key: str) -> dict[str, str]:
        self._purge_expired()
        record = self._hashes.get(key)
        if record is None:
            return {}
        return dict(record.value)

    async def hset(self, key: str, mapping: dict[str, str]) -> int:
        self._purge_expired()
        record = self._hashes.get(key)
        if record is None:
            self._hashes[key] = _HashRecord(value=dict(mapping))
            return len(mapping)
        record.value.update(mapping)
        return len(mapping)

    def pipeline(self) -> "FakeRedisPipeline":
        return FakeRedisPipeline(self)

    def seed_gateway_token(
        self,
        token_hash: str,
        *,
        gateway_id: str,
        status: str = "active",
        label: str | None = None,
        created_at: str | None = None,
    ) -> None:
        value = {
            "gateway_id": gateway_id,
            "status": status,
        }
        if label is not None:
            value["label"] = label
        if created_at is not None:
            value["created_at"] = created_at
        self._hashes[f"facilitator:gateway_token:{token_hash}"] = _HashRecord(value=value)

    def get_string(self, key: str) -> str | None:
        self._purge_expired()
        return self._get_string_value(key)

    def _purge_expired(self) -> None:
        expired_string_keys = [
            key
            for key, record in self._strings.items()
            if record.expires_at is not None and record.expires_at <= self._now
        ]
        for key in expired_string_keys:
            self._strings.pop(key, None)

        expired_hash_keys = [
            key
            for key, record in self._hashes.items()
            if record.expires_at is not None and record.expires_at <= self._now
        ]
        for key in expired_hash_keys:
            self._hashes.pop(key, None)

    def _expires_at(self, ttl_seconds: int | None) -> float | None:
        if ttl_seconds is None:
            return None
        return self._now + ttl_seconds

    def _get_string_value(self, key: str) -> str | None:
        record = self._strings.get(key)
        if record is None:
            return None
        return record.value


class FakeRedisPipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._watched_keys: tuple[str, ...] = ()
        self._watched_values: dict[str, str | None] = {}
        self._commands: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> "FakeRedisPipeline":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self._watched_keys = ()
        self._watched_values = {}
        self._commands.clear()

    async def watch(self, *keys: str) -> None:
        self._redis._purge_expired()
        self._watched_keys = keys
        self._watched_values = {
            key: self._redis.get_string(key)
            for key in keys
        }

    async def mget(self, *keys: str) -> list[str | None]:
        return await self._redis.mget(*keys)

    def multi(self) -> None:
        self._commands = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._commands.append(("set", (key, value, ex)))

    def delete(self, key: str) -> None:
        self._commands.append(("delete", (key,)))

    async def execute(self) -> list[Any]:
        self._redis._purge_expired()
        for key in self._watched_keys:
            if self._redis.get_string(key) != self._watched_values.get(key):
                raise FakeWatchError("watched key changed")

        results: list[Any] = []
        for command, args in self._commands:
            if command == "set":
                results.append(await self._redis.set(*args))
            elif command == "delete":
                results.append(await self._redis.delete(*args))
        return results

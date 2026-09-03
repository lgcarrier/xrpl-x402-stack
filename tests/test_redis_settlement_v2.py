from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Lock
from uuid import uuid4

import pytest
from xrpl.wallet import Wallet

from tests.test_exact_xrpl_v2 import (
    FakeXRPL,
    service,
    signed_payload,
    xrp_requirements,
)
from xrpl_x402_core import XRPLSettlementState
from xrpl_x402_facilitator.replay_store import RedisSettlementStore


class SharedRedisServer:
    """Thread-safe Redis SET/GET semantics shared by independent clients."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._ttls: dict[str, int | None] = {}
        self._lock = Lock()

    def client(self) -> RedisClient:
        return RedisClient(self)


class RedisClient:
    def __init__(self, server: SharedRedisServer) -> None:
        self._server = server

    def get(self, key: str) -> str | None:
        with self._server._lock:
            return self._server._values.get(key)

    def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        with self._server._lock:
            exists = key in self._server._values
            if nx and exists:
                return None
            if xx and not exists:
                return None
            self._server._values[key] = value
            self._server._ttls[key] = ex
            return True

    def eval(
        self,
        script: str,
        numkeys: int,
        key: str,
        candidate_json: str,
        ttl_seconds: int,
    ) -> int:
        assert "candidate_rank > current_rank" in script
        assert numkeys == 1
        with self._server._lock:
            current_json = self._server._values.get(key)
            if current_json is None:
                return 0
            current = json.loads(current_json)
            candidate = json.loads(candidate_json)
            ranks = {"pending": 0, "failed": 1, "validated": 2}
            current_rank = ranks[current["status"]]
            candidate_rank = ranks[candidate["status"]]
            if (
                current["status"] == "pending"
                and candidate["status"] == "pending"
            ):
                self._server._values[key] = candidate_json
                self._server._ttls[key] = None
                return 1
            if candidate_rank > current_rank:
                self._server._values[key] = candidate_json
                self._server._ttls[key] = int(ttl_seconds)
                return 1
            if current["status"] != "pending":
                self._server._ttls[key] = int(ttl_seconds)
            return 0

    def ttl(self, key: str) -> int:
        with self._server._lock:
            if key not in self._server._values:
                return -2
            ttl = self._server._ttls.get(key)
            return -1 if ttl is None else ttl

    def delete(self, key: str) -> None:
        with self._server._lock:
            self._server._values.pop(key, None)
            self._server._ttls.pop(key, None)


def settlement_state(
    transaction: str,
    status: str,
    result: dict[str, str] | None = None,
) -> XRPLSettlementState:
    return XRPLSettlementState(
        transaction=transaction,
        network="xrpl:1",
        payer=Wallet.create().classic_address,
        first_ledger_sequence=1000,
        last_ledger_sequence=1014,
        payment_fingerprint="fingerprint",
        status=status,
        result=result,
    )


def test_redis_store_coordinates_concurrent_facilitators_and_survives_restart(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )
    wallet = Wallet.create()
    accepted = xrp_requirements()
    payload = signed_payload(wallet, accepted)
    rpc = FakeXRPL(wallet)
    redis_server = SharedRedisServer()

    first = service(
        rpc,
        store=RedisSettlementStore(redis_server.client()),
    )
    second = service(
        rpc,
        store=RedisSettlementStore(redis_server.client()),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda mechanism: mechanism.settle(payload, accepted),
                (first, second),
            )
        )

    assert all(
        result.error_reason == "settlement_pending" for result in results
    )
    assert results[0].transaction == results[1].transaction
    assert results[0].transaction
    assert rpc.submit_count == 1

    restarted = service(
        rpc,
        store=RedisSettlementStore(redis_server.client()),
    )
    reconciled = restarted.settle(payload, accepted)

    assert reconciled.error_reason == "settlement_pending"
    assert reconciled.transaction == results[0].transaction
    assert rpc.submit_count == 1

    rpc.tx_result = {
        "validated": True,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": "1000",
        },
    }
    validated = restarted.settle(payload, accepted)
    after_second_restart = service(
        rpc,
        store=RedisSettlementStore(redis_server.client()),
    ).settle(payload, accepted)

    assert validated.success is True
    assert after_second_restart == validated
    assert rpc.submit_count == 1


def test_redis_updates_are_atomic_monotonic_and_pending_has_no_ttl() -> None:
    server = SharedRedisServer()
    client = server.client()
    store = RedisSettlementStore(client)
    transaction = "A" * 64
    pending = settlement_state(transaction, "pending")
    failed = pending.model_copy(
        update={
            "status": "failed",
            "result": {"errorReason": "transaction_expired"},
        }
    )
    validated = pending.model_copy(
        update={"status": "validated", "result": {"amount": "1000"}}
    )

    assert store.reserve(pending, 1) is True
    key = RedisSettlementStore._key(transaction)
    assert client.ttl(key) == -1

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(
            pool.map(
                lambda state: store.update(state, 600),
                (validated, failed, pending),
            )
        )

    assert store.get(transaction) == validated
    store.update(failed, 900)
    store.update(pending, 900)
    assert store.get(transaction) == validated
    assert client.ttl(key) == 900


@pytest.mark.redis
def test_real_redis_atomic_monotonic_update_entrypoint() -> None:
    url = os.environ.get("XRPL_X402_TEST_REDIS_URL")
    if not url:
        pytest.skip("set XRPL_X402_TEST_REDIS_URL to exercise real Redis")
    import redis

    client = redis.from_url(url, decode_responses=True)
    client.ping()
    store = RedisSettlementStore(client)
    transaction = uuid4().hex.upper().ljust(64, "0")
    key = RedisSettlementStore._key(transaction)
    pending = settlement_state(transaction, "pending")
    failed = pending.model_copy(
        update={
            "status": "failed",
            "result": {"errorReason": "transaction_expired"},
        }
    )
    validated = pending.model_copy(
        update={"status": "validated", "result": {"amount": "1000"}}
    )
    try:
        assert store.reserve(pending, 1) is True
        assert client.ttl(key) == -1
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda state: store.update(state, 600),
                    (failed, validated),
                )
            )
        assert store.get(transaction) == validated
        store.update(failed, 600)
        assert store.get(transaction) == validated
        assert client.ttl(key) > 0
    finally:
        client.delete(key)

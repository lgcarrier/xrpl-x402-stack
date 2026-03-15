from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from types import SimpleNamespace

import pytest
from xrpl.core import binarycodec
from xrpl.core.keypairs import sign as keypairs_sign
from xrpl.models.transactions import Payment
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrpl_x402_core import RLUSD_HEX, RLUSD_TESTNET_ISSUER, USDC_HEX, USDC_TESTNET_ISSUER
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.models import SettleResponse
from xrpl_x402_facilitator.replay_store import RedisReplayStore
from xrpl_x402_facilitator.xrpl_service import XRPLService
from tests.fakes import FakeRedis

TEST_DESTINATION = "rTESTDESTINATIONADDRESS123456789"
TEST_VALID_DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_BEARER_TOKEN = "test-facilitator-token"


@dataclass
class FakePayment:
    destination: str
    amount: object
    account: str = TEST_VALID_DESTINATION
    invoice_id: str | None = "INVOICE-123"
    flags: int = 0
    last_ledger_sequence: int | None = None
    tx_hash: str = "ABC123"

    def get_hash(self) -> str:
        return self.tx_hash


def build_service(
    redis_client: FakeRedis | None = None,
    **overrides: object,
) -> XRPLService:
    active_redis = redis_client or FakeRedis()
    settings_data = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": TEST_DESTINATION,
        "NETWORK_ID": "xrpl:1",
        "SETTLEMENT_MODE": "validated",
        "VALIDATION_TIMEOUT": 1,
        "FACILITATOR_BEARER_TOKEN": DEFAULT_BEARER_TOKEN,
        "REDIS_URL": "redis://fake:6379/0",
        **overrides,
    }
    settings = Settings(**settings_data)
    return XRPLService(settings, redis_client=active_redis)


def build_public_service(
    redis_client: FakeRedis | None = None,
    **overrides: object,
) -> tuple[XRPLService, FakeRedis]:
    active_redis = redis_client or FakeRedis()
    settings_data = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": TEST_DESTINATION,
        "NETWORK_ID": "xrpl:1",
        "SETTLEMENT_MODE": "validated",
        "VALIDATION_TIMEOUT": 1,
        "GATEWAY_AUTH_MODE": "redis_gateways",
        "REDIS_URL": "redis://fake:6379/0",
        "FACILITATOR_BEARER_TOKEN": None,
        **overrides,
    }
    settings = Settings(**settings_data)
    return XRPLService(settings, redis_client=active_redis), active_redis


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


def build_account_mismatch_signed_blob(
    *,
    destination: str = TEST_VALID_DESTINATION,
    amount: object = "2000000",
) -> str:
    signer_wallet = Wallet.create()
    account_wallet = Wallet.create()
    payment = Payment(
        account=account_wallet.classic_address,
        destination=destination,
        amount=amount,
        fee="12",
        sequence=1,
        signing_pub_key=signer_wallet.public_key,
    )
    tx_dict = payment.to_xrpl()
    tx_dict["TxnSignature"] = keypairs_sign(
        bytes.fromhex(binarycodec.encode_for_signing(tx_dict)),
        signer_wallet.private_key,
    )
    return binarycodec.encode(tx_dict)


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


def test_verify_normalizes_rlusd_hex_currency_code() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "1.5",
        },
    )

    response = asyncio.run(service.verify_payment(signed_blob))

    assert response.asset.model_dump() == {
        "code": "RLUSD",
        "issuer": RLUSD_TESTNET_ISSUER,
    }
    assert response.amount == "1.5 RLUSD"
    assert response.amount_details.model_dump() == {
        "value": "1.5",
        "unit": "issued",
        "asset": {"code": "RLUSD", "issuer": RLUSD_TESTNET_ISSUER},
        "drops": None,
    }
    assert response.payer == binarycodec.decode(signed_blob)["Account"]
    assert response.invoice_id == hashlib.sha256(signed_blob.encode("utf-8")).hexdigest()[:32]


def test_verify_normalizes_usdc_hex_currency_code() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": USDC_HEX,
            "issuer": USDC_TESTNET_ISSUER,
            "value": "2.25",
        },
    )

    response = asyncio.run(service.verify_payment(signed_blob))

    assert response.asset.model_dump() == {
        "code": "USDC",
        "issuer": USDC_TESTNET_ISSUER,
    }
    assert response.amount == "2.25 USDC"
    assert response.amount_details.model_dump() == {
        "value": "2.25",
        "unit": "issued",
        "asset": {"code": "USDC", "issuer": USDC_TESTNET_ISSUER},
        "drops": None,
    }
    assert response.payer == binarycodec.decode(signed_blob)["Account"]
    assert response.invoice_id == hashlib.sha256(signed_blob.encode("utf-8")).hexdigest()[:32]


def test_verify_rejects_zero_value_issued_asset() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "0",
        },
    )

    with pytest.raises(ValueError, match="greater than zero"):
        asyncio.run(service.verify_payment(signed_blob))


def test_verify_rejects_signing_pub_key_account_mismatch() -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)

    with pytest.raises(ValueError, match="SigningPubKey does not match Account"):
        asyncio.run(
            service.verify_payment(
                build_account_mismatch_signed_blob(
                    destination=TEST_VALID_DESTINATION,
                )
            )
        )


def test_repeated_verify_allowed_before_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    first_response = asyncio.run(service.verify_payment("blob"))
    second_response = asyncio.run(service.verify_payment("blob"))

    assert first_response.invoice_id == "INVOICE-123"
    assert second_response.invoice_id == "INVOICE-123"


def test_public_mode_verify_rejects_missing_last_ledger_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _redis_client = build_public_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    with pytest.raises(ValueError, match="LastLedgerSequence required"):
        asyncio.run(service.verify_payment("blob"))


def test_public_mode_verify_rejects_expired_last_ledger_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _redis_client = build_public_service()
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=100,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service, monkeypatch, 100)

    with pytest.raises(ValueError, match="LastLedgerSequence expired"):
        asyncio.run(service.verify_payment("blob"))


def test_public_mode_verify_rejects_future_last_ledger_sequence_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _redis_client = build_public_service()
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=121,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service, monkeypatch, 100)

    with pytest.raises(ValueError, match="too far in the future"):
        asyncio.run(service.verify_payment("blob"))


def test_public_mode_verify_accepts_last_ledger_sequence_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _redis_client = build_public_service()
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=120,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service, monkeypatch, 100)

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


@pytest.mark.parametrize(
    ("last_ledger_sequence", "current_ledger", "expected_message"),
    [
        (None, None, "LastLedgerSequence required"),
        (100, 100, "LastLedgerSequence expired"),
        (121, 100, "too far in the future"),
    ],
)
def test_public_mode_settle_rejects_invalid_last_ledger_sequence(
    last_ledger_sequence: int | None,
    current_ledger: int | None,
    expected_message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _redis_client = build_public_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=last_ledger_sequence,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    if current_ledger is not None:
        set_validated_ledger_sequence(service, monkeypatch, current_ledger)

    with pytest.raises(ValueError, match=expected_message):
        asyncio.run(service.settle_payment("blob"))


def test_public_mode_settle_accepts_last_ledger_sequence_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _redis_client = build_public_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=120,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service, monkeypatch, 100)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    response = asyncio.run(service.settle_payment("blob"))

    assert response.model_dump() == {
        "settled": True,
        "tx_hash": "ABC123",
        "status": "submitted",
    }


def test_settle_validated_xrp_payment_checks_delivered_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {"validated": True, "meta": {"delivered_amount": "2000000"}},
    )

    response = asyncio.run(service.settle_payment("blob"))

    assert response.model_dump() == {
        "settled": True,
        "tx_hash": "ABC123",
        "status": "validated",
    }


def test_settle_validated_rlusd_payment_accepts_hex_delivered_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "3.75",
        },
    )
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {
            "validated": True,
            "meta": {
                "delivered_amount": {
                    "currency": RLUSD_HEX,
                    "issuer": RLUSD_TESTNET_ISSUER,
                    "value": "3.75",
                }
            },
        },
    )

    response = asyncio.run(service.settle_payment(signed_blob))

    assert response.model_dump() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "validated",
    }


def test_settle_validated_usdc_payment_accepts_hex_delivered_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": USDC_HEX,
            "issuer": USDC_TESTNET_ISSUER,
            "value": "4.5",
        },
    )
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {
            "validated": True,
            "meta": {
                "delivered_amount": {
                    "currency": USDC_HEX,
                    "issuer": USDC_TESTNET_ISSUER,
                    "value": "4.5",
                }
            },
        },
    )

    response = asyncio.run(service.settle_payment(signed_blob))

    assert response.model_dump() == {
        "settled": True,
        "tx_hash": tx_hash,
        "status": "validated",
    }


def test_verify_rejects_pending_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    async def run_test() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _client_request(_request: object) -> object:
            started.set()
            await release.wait()
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})

        monkeypatch.setattr(service, "_client_request", _client_request)
        settlement_task = asyncio.create_task(service.settle_payment("blob"))
        await started.wait()
        with pytest.raises(ValueError, match="replay attack"):
            await service.verify_payment("blob")
        release.set()
        result = await settlement_task
        assert result.status == "submitted"

    asyncio.run(run_test())


def test_concurrent_settle_rejects_duplicate_in_flight_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    client_requests = 0

    async def run_test() -> list[SettleResponse | Exception]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _client_request(_request: object) -> object:
            nonlocal client_requests
            client_requests += 1
            started.set()
            await release.wait()
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})

        monkeypatch.setattr(service, "_client_request", _client_request)
        first = asyncio.create_task(service.settle_payment("blob"))
        await started.wait()
        second = asyncio.create_task(service.settle_payment("blob"))
        release.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = asyncio.run(run_test())

    successes = [result for result in results if isinstance(result, SettleResponse)]
    failures = [result for result in results if isinstance(result, Exception)]

    assert len(successes) == 1
    assert successes[0].status == "submitted"
    assert len(failures) == 1
    assert "replay attack" in str(failures[0]).lower()
    assert client_requests == 1


def test_public_mode_replay_is_shared_across_service_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    service_a, _ = build_public_service(
        redis_client=redis_client,
        SETTLEMENT_MODE="optimistic",
    )
    service_b, _ = build_public_service(
        redis_client=redis_client,
        SETTLEMENT_MODE="optimistic",
    )
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=110,
    )
    monkeypatch.setattr(service_a, "_decode_payment", lambda _blob: payment)
    monkeypatch.setattr(service_b, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service_a, monkeypatch, 100)
    set_validated_ledger_sequence(service_b, monkeypatch, 100)
    set_client_responses(
        service_a,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    response = asyncio.run(service_a.settle_payment("blob"))

    assert response.status == "submitted"
    with pytest.raises(ValueError, match="replay attack"):
        asyncio.run(service_b.verify_payment("blob"))


def test_public_mode_processed_replay_markers_survive_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    service, _ = build_public_service(
        redis_client=redis_client,
        SETTLEMENT_MODE="optimistic",
    )
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=110,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service, monkeypatch, 100)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    response = asyncio.run(service.settle_payment("blob"))

    assert response.status == "submitted"

    restarted_service, _ = build_public_service(redis_client=redis_client)
    monkeypatch.setattr(restarted_service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(restarted_service, monkeypatch, 100)

    with pytest.raises(ValueError, match="replay attack"):
        asyncio.run(restarted_service.verify_payment("blob"))


def test_public_mode_processed_replay_markers_expire_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, redis_client = build_public_service(
        SETTLEMENT_MODE="optimistic",
        REPLAY_PROCESSED_TTL_SECONDS=5,
    )
    payment = FakePayment(
        destination=TEST_DESTINATION,
        amount="2000000",
        last_ledger_sequence=110,
    )
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_validated_ledger_sequence(service, monkeypatch, 100)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    response = asyncio.run(service.settle_payment("blob"))

    assert response.status == "submitted"

    with pytest.raises(ValueError, match="replay attack"):
        asyncio.run(service.verify_payment("blob"))

    redis_client.advance(6)
    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_single_token_mode_processed_replay_markers_expire_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    service = build_service(
        redis_client=redis_client,
        SETTLEMENT_MODE="optimistic",
        REPLAY_PROCESSED_TTL_SECONDS=5,
    )

    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    response = asyncio.run(service.settle_payment("blob"))

    assert response.status == "submitted"

    with pytest.raises(ValueError, match="replay attack"):
        asyncio.run(service.verify_payment("blob"))

    redis_client.advance(6)
    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_single_token_mode_replay_is_shared_across_service_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = FakeRedis()
    service_a = build_service(redis_client=redis_client, SETTLEMENT_MODE="optimistic")
    service_b = build_service(redis_client=redis_client, SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service_a, "_decode_payment", lambda _blob: payment)
    monkeypatch.setattr(service_b, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service_a,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
    )

    response = asyncio.run(service_a.settle_payment("blob"))

    assert response.status == "submitted"
    with pytest.raises(ValueError, match="replay attack"):
        asyncio.run(service_b.verify_payment("blob"))


def test_redis_replay_release_pending_only_releases_matching_reservation() -> None:
    redis_client = FakeRedis()
    replay_store = RedisReplayStore(
        redis_client,
        processed_ttl_seconds=604800,
        pending_ttl_seconds=300,
    )
    reservation = asyncio.run(replay_store.reserve("invoice-1", "blob-1"))
    replacement_value = "pending:replacement"

    asyncio.run(
        redis_client.set(
            RedisReplayStore._invoice_key("invoice-1"),
            replacement_value,
            ex=300,
        )
    )
    asyncio.run(
        redis_client.set(
            RedisReplayStore._blob_key("blob-1"),
            replacement_value,
            ex=300,
        )
    )

    asyncio.run(replay_store.release_pending(reservation))

    assert (
        redis_client.get_string(RedisReplayStore._invoice_key("invoice-1"))
        == replacement_value
    )
    assert (
        redis_client.get_string(RedisReplayStore._blob_key("blob-1"))
        == replacement_value
    )


def test_settle_validated_payment_rejects_missing_delivered_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {"validated": True, "meta": {}},
    )

    with pytest.raises(ValueError, match="missing delivered_amount"):
        asyncio.run(service.settle_payment("blob"))


def test_settle_validated_payment_rejects_mismatched_xrp_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {"validated": True, "meta": {"delivered_amount": "1999999"}},
    )

    with pytest.raises(ValueError, match="wrong XRP amount"):
        asyncio.run(service.settle_payment("blob"))


def test_settle_validated_payment_rejects_mismatched_issued_asset_issuer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(MY_DESTINATION_ADDRESS=TEST_VALID_DESTINATION)
    signed_blob, _tx_hash = build_signed_payment_blob(
        destination=TEST_VALID_DESTINATION,
        amount={
            "currency": RLUSD_HEX,
            "issuer": RLUSD_TESTNET_ISSUER,
            "value": "3.75",
        },
    )
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {
            "validated": True,
            "meta": {
                "delivered_amount": {
                    "currency": "RLUSD",
                    "issuer": "rWrongIssuer123456789",
                    "value": "3.75",
                }
            },
        },
    )

    with pytest.raises(ValueError, match="unexpected asset"):
        asyncio.run(service.settle_payment(signed_blob))


def test_submission_rejection_releases_pending_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tecPATH_DRY"},
    )

    with pytest.raises(ValueError, match="XRPL submission rejected"):
        asyncio.run(service.settle_payment("blob"))

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_submission_status_error_releases_pending_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    async def _client_request(_request: object) -> object:
        return SimpleNamespace(
            status="error",
            result={
                "error": "upstreamFailure",
                "error_message": "upstream says no",
            },
        )

    monkeypatch.setattr(service, "_client_request", _client_request)

    with pytest.raises(ValueError, match="XRPL submission failed: upstream says no"):
        asyncio.run(service.settle_payment("blob"))

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_submission_missing_engine_result_releases_pending_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    async def _client_request(_request: object) -> object:
        return SimpleNamespace(
            status="success",
            result={"error": "upstreamFailure"},
        )

    monkeypatch.setattr(service, "_client_request", _client_request)

    with pytest.raises(
        ValueError,
        match=r"XRPL submission failed: missing engine_result \(upstreamFailure\)",
    ):
        asyncio.run(service.settle_payment("blob"))

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_validated_mode_rejects_malformed_submit_response_before_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    call_count = 0

    async def _client_request(_request: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise AssertionError("Validation polling should not start after submit failure")
        return SimpleNamespace(status="success", result={})

    monkeypatch.setattr(service, "_client_request", _client_request)

    with pytest.raises(ValueError, match="XRPL submission failed: missing engine_result"):
        asyncio.run(service.settle_payment("blob"))

    assert call_count == 1


def test_submission_rpc_error_releases_pending_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(SETTLEMENT_MODE="optimistic")
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    async def _client_request(_request: object) -> object:
        raise RuntimeError("rpc down")

    monkeypatch.setattr(service, "_client_request", _client_request)

    with pytest.raises(ValueError, match="rpc down"):
        asyncio.run(service.settle_payment("blob"))

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_validation_timeout_releases_pending_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service(VALIDATION_TIMEOUT=1)
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {"validated": False},
    )

    with pytest.raises(ValueError, match="Validation timeout exceeded"):
        asyncio.run(service.settle_payment("blob"))

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_validation_rpc_error_releases_pending_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)

    responses = [SimpleNamespace(result={"engine_result": "tesSUCCESS"})]

    async def _client_request(_request: object) -> object:
        if responses:
            return responses.pop(0)
        raise RuntimeError("rpc down")

    monkeypatch.setattr(service, "_client_request", _client_request)

    with pytest.raises(ValueError, match="rpc down"):
        asyncio.run(service.settle_payment("blob"))

    response = asyncio.run(service.verify_payment("blob"))

    assert response.valid is True


def test_terminal_settlement_failure_marks_blob_as_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()
    payment = FakePayment(destination=TEST_DESTINATION, amount="2000000")
    monkeypatch.setattr(service, "_decode_payment", lambda _blob: payment)
    set_client_responses(
        service,
        monkeypatch,
        {"engine_result": "tesSUCCESS"},
        {"validated": True, "meta": {}},
    )

    with pytest.raises(ValueError, match="missing delivered_amount"):
        asyncio.run(service.settle_payment("blob"))

    with pytest.raises(ValueError, match="replay attack"):
        asyncio.run(service.verify_payment("blob"))

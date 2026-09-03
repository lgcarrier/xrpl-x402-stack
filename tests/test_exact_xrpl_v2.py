from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from x402.schemas import PaymentPayload, PaymentRequirements
from xrpl.core import binarycodec
from xrpl.models.requests import (
    AccountInfo,
    AccountLines,
    AccountObjects,
    Ledger,
    Simulate,
    SubmitOnly,
    ServerState,
    Tx,
)
from xrpl.models.transactions import Memo, Payment
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner
from xrpl_x402_core import (
    RLUSD_HEX,
    RLUSD_TESTNET_ISSUER,
    TF_PARTIAL_PAYMENT,
    invoice_id_to_invoice_id_field,
)
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.replay_store import InMemorySettlementStore
from xrpl_x402_facilitator.xrpl_service import ExactXRPLFacilitatorScheme

CURRENT_LEDGER = 1000
DESTINATION = Wallet.create().classic_address
TOKEN = "test-token"


def settings(**overrides: Any) -> Settings:
    values = {
        "MY_DESTINATION_ADDRESS": DESTINATION,
        "FACILITATOR_BEARER_TOKEN": TOKEN,
        "REDIS_URL": "redis://unused:6379/0",
        "NETWORK_ID": "xrpl:1",
        "VALIDATION_TIMEOUT": 1,
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        **values,
    )


class FakeXRPL:
    def __init__(
        self,
        wallet: Wallet,
        *,
        sequence: int = 7,
        regular_key: str | None = None,
        flags: int = 0,
        ticket: int | None = None,
        tx_result: dict[str, Any] | None = None,
        simulation_error: bool = False,
        simulation_response: dict[str, Any] | None = None,
        simulation_result: str = "tesSUCCESS",
        submit_result: str = "tesSUCCESS",
        balance: str = "100000000",
        trustline_balance: str = "100",
        owner_count: int = 0,
        reserve_base: int = 1_000_000,
        reserve_increment: int = 200_000,
        destination_exists: bool = True,
        transfer_rate: int | None = None,
    ) -> None:
        self.wallet = wallet
        self.sequence = sequence
        self.regular_key = regular_key
        self.flags = flags
        self.ticket = ticket
        self.tx_result = tx_result
        self.simulation_error = simulation_error
        self.simulation_response = simulation_response
        self.simulation_result = simulation_result
        self.submit_result = submit_result
        self.balance = balance
        self.trustline_balance = trustline_balance
        self.owner_count = owner_count
        self.reserve_base = reserve_base
        self.reserve_increment = reserve_increment
        self.destination_exists = destination_exists
        self.transfer_rate = transfer_rate
        self.submit_count = 0

    def request(self, request: Any) -> SimpleNamespace:
        if isinstance(request, Ledger):
            return SimpleNamespace(result={"ledger_index": CURRENT_LEDGER})
        if isinstance(request, AccountInfo):
            if (
                request.account == DESTINATION
                and request.account != self.wallet.classic_address
                and not self.destination_exists
            ):
                return SimpleNamespace(result={"error": "actNotFound"})
            data = {
                "Sequence": self.sequence,
                "Balance": self.balance,
                "Flags": self.flags,
                "OwnerCount": self.owner_count,
            }
            if self.transfer_rate is not None:
                data["TransferRate"] = self.transfer_rate
            if self.regular_key:
                data["RegularKey"] = self.regular_key
            return SimpleNamespace(result={"account_data": data})
        if isinstance(request, AccountObjects):
            objects = (
                [{"TicketSequence": self.ticket}]
                if self.ticket is not None
                else []
            )
            return SimpleNamespace(result={"account_objects": objects})
        if isinstance(request, Simulate):
            if self.simulation_error:
                raise RuntimeError("simulate is unavailable")
            if self.simulation_response is not None:
                return SimpleNamespace(result=self.simulation_response)
            return SimpleNamespace(
                result={"engine_result": self.simulation_result}
            )
        if isinstance(request, AccountLines):
            return SimpleNamespace(
                result={
                    "lines": [
                        {
                            "account": request.peer,
                            "currency": RLUSD_HEX,
                            "balance": self.trustline_balance,
                        }
                    ]
                }
            )
        if isinstance(request, ServerState):
            return SimpleNamespace(
                result={
                    "state": {
                        "validated_ledger": {
                            "reserve_base": self.reserve_base,
                            "reserve_inc": self.reserve_increment,
                        }
                    }
                }
            )
        if isinstance(request, SubmitOnly):
            self.submit_count += 1
            return SimpleNamespace(result={"engine_result": self.submit_result})
        if isinstance(request, Tx):
            if self.tx_result is None:
                raise RuntimeError("txnNotFound")
            return SimpleNamespace(result=self.tx_result)
        raise AssertionError(type(request))


def xrp_requirements(**extra_updates: Any) -> PaymentRequirements:
    extra = {
        "areFeesSponsored": False,
        "assetTransferMethod": "sequence",
    }
    extra.update(extra_updates)
    return PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra=extra,
    )


def signed_payload(
    payer: Wallet,
    requirements: PaymentRequirements,
    *,
    signing_wallet: Wallet | None = None,
    sequence: int = 7,
    ticket: int | None = None,
    fee: str = "12",
    last_ledger: int | None = 1014,
    memos: list[Memo] | None = None,
    network_id: int | None = None,
    flags: int | None = None,
    destination: str | None = None,
    destination_tag: int | None = None,
    invoice_id: str | None = None,
    send_max_override: Any = ...,
    deliver_min: Any = None,
    delegate: str | None = None,
    paths: Any = None,
) -> PaymentPayload:
    extra = requirements.extra or {}
    amount: Any = requirements.amount
    send_max: Any = None
    if requirements.asset != "XRP":
        amount = {
            "currency": requirements.asset,
            "issuer": extra["issuer"],
            "value": requirements.amount,
        }
        send_max = dict(amount)
    if send_max_override is not ...:
        send_max = send_max_override
    tx = Payment(
        account=payer.classic_address,
        destination=destination or requirements.pay_to,
        amount=amount,
        send_max=send_max,
        sequence=0 if ticket is not None else sequence,
        ticket_sequence=ticket,
        fee=fee,
        last_ledger_sequence=last_ledger,
        destination_tag=(
            destination_tag
            if destination_tag is not None
            else extra.get("destinationTag")
        ),
        invoice_id=(
            invoice_id
            if invoice_id is not None
            else invoice_id_to_invoice_id_field(extra["invoiceId"])
            if extra.get("invoiceId")
            else None
        ),
        network_id=network_id,
        memos=memos,
        flags=flags,
        deliver_min=deliver_min,
        delegate=delegate,
        paths=paths,
    )
    blob = sign(tx, signing_wallet or payer).blob().upper()
    return PaymentPayload(
        payload={"signedTxBlob": blob},
        accepted=requirements,
    )


def service(
    rpc: FakeXRPL,
    *,
    store: InMemorySettlementStore | None = None,
    **setting_overrides: Any,
) -> ExactXRPLFacilitatorScheme:
    return ExactXRPLFacilitatorScheme(
        settings(**setting_overrides),
        client=rpc,
        settlement_store=store or InMemorySettlementStore(),
    )


def test_master_key_xrp_sequence_verifies_with_simulation() -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements(
        invoiceId="order-42", destinationTag=123
    )
    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, requirements), requirements
    )
    assert result.is_valid is True
    assert result.payer == wallet.classic_address
    assert result.extra == {"verificationPath": "simulation"}


def test_regular_key_and_ticket_authorization_flow() -> None:
    master = Wallet.create()
    regular = Wallet.create()
    requirements = xrp_requirements(assetTransferMethod="ticketSequence")
    rpc = FakeXRPL(
        master,
        regular_key=regular.classic_address,
        flags=0x00100000,
        ticket=55,
    )
    result = service(rpc).verify(
        signed_payload(
            master,
            requirements,
            signing_wallet=regular,
            ticket=55,
        ),
        requirements,
    )
    assert result.is_valid is True


def test_ticket_verification_paginates_account_objects() -> None:
    class PaginatedTicketXRPL(FakeXRPL):
        def __init__(self, wallet: Wallet) -> None:
            super().__init__(wallet)
            self.markers: list[Any] = []

        def request(self, request: Any) -> SimpleNamespace:
            if isinstance(request, AccountObjects):
                self.markers.append(request.marker)
                if request.marker is None:
                    return SimpleNamespace(
                        result={"account_objects": [], "marker": "next"}
                    )
                return SimpleNamespace(
                    result={"account_objects": [{"TicketSequence": 55}]}
                )
            return super().request(request)

    wallet = Wallet.create()
    requirements = xrp_requirements(
        assetTransferMethod="ticketSequence"
    )
    rpc = PaginatedTicketXRPL(wallet)

    result = service(rpc).verify(
        signed_payload(wallet, requirements, ticket=55), requirements
    )

    assert result.is_valid is True
    assert rpc.markers == [None, "next"]


def test_rejects_missing_ledger_expiry_and_unapproved_signer() -> None:
    wallet = Wallet.create()
    accepted = xrp_requirements()
    missing_expiry = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, accepted, last_ledger=None),
        accepted,
    )
    unauthorized = service(FakeXRPL(wallet)).verify(
        signed_payload(
            wallet,
            accepted,
            signing_wallet=Wallet.create(),
        ),
        accepted,
    )

    assert missing_expiry.is_valid is False
    assert "lastledgersequence_missing" in (
        missing_expiry.invalid_reason or ""
    )
    assert unauthorized.is_valid is False
    assert "signer_not_authorized" in (unauthorized.invalid_reason or "")


def test_iou_amount_sendmax_and_simulation_fallback() -> None:
    wallet = Wallet.create()
    requirements = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    rpc = FakeXRPL(wallet)
    result = service(rpc).verify(
        signed_payload(wallet, requirements), requirements
    )
    assert result.is_valid is True
    decoded = binarycodec.decode(
        signed_payload(wallet, requirements).payload["signedTxBlob"]
    )
    assert decoded["Amount"] == decoded["SendMax"]


def test_simulation_unavailable_uses_targeted_xrp_balance_check() -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements()
    result = service(FakeXRPL(wallet, simulation_error=True)).verify(
        signed_payload(wallet, requirements), requirements
    )
    assert result.is_valid is True
    assert result.extra == {"verificationPath": "targetedChecks"}


@pytest.mark.parametrize(
    "simulation_response",
    [
        {"error": "unknownCmd", "error_message": "Unknown method."},
        {
            "error": {
                "code": -32601,
                "message": "Method not found",
            }
        },
    ],
)
def test_simulation_method_unavailable_response_uses_targeted_checks(
    simulation_response: dict[str, Any],
) -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements()
    result = service(
        FakeXRPL(wallet, simulation_response=simulation_response)
    ).verify(signed_payload(wallet, requirements), requirements)

    assert result.is_valid is True
    assert result.extra == {"verificationPath": "targetedChecks"}


def test_simulation_rpc_error_response_fails_closed() -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements()
    result = service(
        FakeXRPL(
            wallet,
            simulation_response={
                "error": "invalidParams",
                "error_message": "Invalid transaction",
            },
        )
    ).verify(signed_payload(wallet, requirements), requirements)

    assert result.is_valid is False
    assert "simulation_failed" in (result.invalid_reason or "")


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"fee": "1001"}, "fee_too_high"),
        ({"last_ledger": 1000}, "expired"),
        ({"last_ledger": 1015}, "lastledgersequence_too_large"),
        ({"sequence": 8}, "sequence_not_current"),
    ],
)
def test_rejects_fee_expiry_and_sequence_violations(
    mutation: dict[str, Any], reason: str
) -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements()
    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, requirements, **mutation), requirements
    )
    assert result.is_valid is False
    assert reason in (result.invalid_reason or "")


def test_rejects_memos_and_malformed_signature() -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements()
    with_memo = signed_payload(
        wallet,
        requirements,
        memos=[Memo(memo_data="CAFE")],
    )
    memo_result = service(FakeXRPL(wallet)).verify(with_memo, requirements)
    assert memo_result.is_valid is False
    assert "unexpected_fields" in (memo_result.invalid_reason or "")

    decoded = binarycodec.decode(
        signed_payload(wallet, requirements).payload["signedTxBlob"]
    )
    decoded["TxnSignature"] = "00" * (len(decoded["TxnSignature"]) // 2)
    bad = PaymentPayload(
        payload={"signedTxBlob": binarycodec.encode(decoded)},
        accepted=requirements,
    )
    signature_result = service(FakeXRPL(wallet)).verify(bad, requirements)
    assert signature_result.is_valid is False
    assert "signature" in (signature_result.invalid_reason or "")


@pytest.mark.parametrize(
    "transaction_updates",
    [
        {"flags": TF_PARTIAL_PAYMENT},
        {"deliver_min": "1"},
        {"delegate": Wallet.create().classic_address},
    ],
)
def test_rejects_prohibited_payment_fields(
    transaction_updates: dict[str, Any],
) -> None:
    wallet = Wallet.create()
    accepted = xrp_requirements()
    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, accepted, **transaction_updates),
        accepted,
    )
    assert result.is_valid is False


def test_rejects_paths_for_iou_payment() -> None:
    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, accepted, paths=[[{"currency": "XRP"}]]),
        accepted,
    )
    assert result.is_valid is False
    assert "unexpected_fields" in (result.invalid_reason or "")


@pytest.mark.parametrize(
    ("transaction_updates", "reason"),
    [
        ({"destination": Wallet.create().classic_address}, "destination_mismatch"),
        ({"destination_tag": 999}, "destination_tag_mismatch"),
        ({"invoice_id": "F" * 64}, "invoice_id_mismatch"),
        ({"network_id": 1}, "network_id_for_standard_network"),
    ],
)
def test_rejects_destination_invoice_tag_and_standard_network_id_mismatches(
    transaction_updates: dict[str, Any], reason: str
) -> None:
    wallet = Wallet.create()
    accepted = xrp_requirements(invoiceId="order-42", destinationTag=123)
    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, accepted, **transaction_updates),
        accepted,
    )
    assert result.is_valid is False
    assert reason in (result.invalid_reason or "")


def test_rejects_disabled_master_and_consumed_ticket() -> None:
    wallet = Wallet.create()
    sequence_requirements = xrp_requirements()
    disabled = service(FakeXRPL(wallet, flags=0x00100000)).verify(
        signed_payload(wallet, sequence_requirements), sequence_requirements
    )
    assert disabled.is_valid is False
    assert "signer_not_authorized" in (disabled.invalid_reason or "")

    ticket_requirements = xrp_requirements(
        assetTransferMethod="ticketSequence"
    )
    consumed = service(FakeXRPL(wallet, ticket=None)).verify(
        signed_payload(wallet, ticket_requirements, ticket=55),
        ticket_requirements,
    )
    assert consumed.is_valid is False
    assert "ticket_not_available" in (consumed.invalid_reason or "")


@pytest.mark.parametrize(
    ("send_max", "reason"),
    [
        (None, "sendmax_required"),
        (
            {
                "currency": RLUSD_HEX,
                "issuer": RLUSD_TESTNET_ISSUER,
                "value": "0.4",
            },
            "sendmax_too_low",
        ),
        (
            {
                "currency": RLUSD_HEX,
                "issuer": Wallet.create().classic_address,
                "value": "0.5",
            },
            "sendmax_iou_mismatch",
        ),
    ],
)
def test_rejects_missing_or_incompatible_iou_sendmax(
    send_max: Any, reason: str
) -> None:
    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, accepted, send_max_override=send_max),
        accepted,
    )
    assert result.is_valid is False
    assert reason in (result.invalid_reason or "")


def test_targeted_checks_cover_xrp_balance_and_iou_trustline() -> None:
    wallet = Wallet.create()
    xrp = xrp_requirements()
    insufficient_xrp = service(
        FakeXRPL(wallet, simulation_error=True, balance="1000")
    ).verify(signed_payload(wallet, xrp), xrp)
    assert insufficient_xrp.is_valid is False
    assert "insufficient_balance" in (insufficient_xrp.invalid_reason or "")

    iou = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    sufficient_iou = service(
        FakeXRPL(wallet, simulation_error=True, trustline_balance="1")
    ).verify(signed_payload(wallet, iou), iou)
    insufficient_iou = service(
        FakeXRPL(wallet, simulation_error=True, trustline_balance="0.1")
    ).verify(signed_payload(wallet, iou), iou)
    assert sufficient_iou.is_valid is True
    assert sufficient_iou.extra == {"verificationPath": "targetedChecks"}
    assert insufficient_iou.is_valid is False
    assert "insufficient_trustline_balance" in (
        insufficient_iou.invalid_reason or ""
    )


def test_targeted_xrp_check_accounts_for_base_and_owner_reserve() -> None:
    wallet = Wallet.create()
    accepted = xrp_requirements()
    reserve = 1_000_000 + 2 * 200_000
    required = int(accepted.amount) + 12
    sufficient = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            balance=str(reserve + required),
            owner_count=2,
        )
    ).verify(signed_payload(wallet, accepted), accepted)
    insufficient = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            balance=str(reserve + required - 1),
            owner_count=2,
        )
    ).verify(signed_payload(wallet, accepted), accepted)

    assert sufficient.is_valid is True
    assert sufficient.extra == {"verificationPath": "targetedChecks"}
    assert insufficient.is_valid is False
    assert "insufficient_balance" in (insufficient.invalid_reason or "")


def test_targeted_xrp_check_enforces_destination_account_creation_reserve() -> None:
    wallet = Wallet.create()
    too_small = xrp_requirements()
    creation_payment = too_small.model_copy(update={"amount": "1000000"})

    rejected = service(
        FakeXRPL(wallet, simulation_error=True, destination_exists=False)
    ).verify(signed_payload(wallet, too_small), too_small)
    accepted = service(
        FakeXRPL(wallet, simulation_error=True, destination_exists=False)
    ).verify(
        signed_payload(wallet, creation_payment),
        creation_payment,
    )

    assert rejected.is_valid is False
    assert "destination_account_creation_amount_too_low" in (
        rejected.invalid_reason or ""
    )
    assert accepted.is_valid is True
    assert accepted.extra == {"verificationPath": "targetedChecks"}


def test_targeted_iou_check_uses_sendmax_for_transfer_fee_headroom() -> None:
    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    send_max = {
        "currency": RLUSD_HEX,
        "issuer": RLUSD_TESTNET_ISSUER,
        "value": "0.6",
    }

    insufficient = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            trustline_balance="0.55",
        )
    ).verify(
        signed_payload(wallet, accepted, send_max_override=send_max),
        accepted,
    )
    sufficient = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            trustline_balance="0.6",
        )
    ).verify(
        signed_payload(wallet, accepted, send_max_override=send_max),
        accepted,
    )

    assert insufficient.is_valid is False
    assert "insufficient_trustline_balance" in (
        insufficient.invalid_reason or ""
    )
    assert sufficient.is_valid is True
    assert sufficient.extra == {"verificationPath": "targetedChecks"}


def test_targeted_iou_check_enforces_issuer_transfer_rate() -> None:
    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    exact_send_max = {
        "currency": RLUSD_HEX,
        "issuer": RLUSD_TESTNET_ISSUER,
        "value": "0.5",
    }
    fee_adjusted_send_max = {
        **exact_send_max,
        "value": "0.6",
    }

    rejected = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            trustline_balance="1",
            transfer_rate=1_200_000_000,
        )
    ).verify(
        signed_payload(
            wallet,
            accepted,
            send_max_override=exact_send_max,
        ),
        accepted,
    )
    accepted_result = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            trustline_balance="1",
            transfer_rate=1_200_000_000,
        )
    ).verify(
        signed_payload(
            wallet,
            accepted,
            send_max_override=fee_adjusted_send_max,
        ),
        accepted,
    )

    assert rejected.is_valid is False
    assert "sendmax_transfer_fee_too_low" in (
        rejected.invalid_reason or ""
    )
    assert accepted_result.is_valid is True
    assert accepted_result.extra == {"verificationPath": "targetedChecks"}


def test_targeted_iou_check_preserves_xrp_fee_reserve() -> None:
    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    result = service(
        FakeXRPL(
            wallet,
            simulation_error=True,
            balance="1000011",
            trustline_balance="1",
        )
    ).verify(signed_payload(wallet, accepted), accepted)

    assert result.is_valid is False
    assert "insufficient_balance" in (result.invalid_reason or "")


def test_targeted_iou_check_paginates_account_lines() -> None:
    class PaginatedLinesXRPL(FakeXRPL):
        def __init__(self, wallet: Wallet) -> None:
            super().__init__(wallet, simulation_error=True)
            self.markers: list[Any] = []

        def request(self, request: Any) -> SimpleNamespace:
            if isinstance(request, AccountLines):
                self.markers.append(request.marker)
                if request.marker is None:
                    return SimpleNamespace(
                        result={"lines": [], "marker": {"page": 2}}
                    )
                return SimpleNamespace(
                    result={
                        "lines": [
                            {
                                "account": request.peer,
                                "currency": RLUSD_HEX,
                                "balance": "1",
                            }
                        ]
                    }
                )
            return super().request(request)

    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    rpc = PaginatedLinesXRPL(wallet)

    result = service(rpc).verify(
        signed_payload(wallet, accepted), accepted
    )

    assert result.is_valid is True
    assert rpc.markers == [None, {"page": 2}]


def test_simulation_failure_and_accepted_requirements_mismatch_are_rejected() -> None:
    wallet = Wallet.create()
    accepted = xrp_requirements()
    simulation = service(
        FakeXRPL(wallet, simulation_result="tecUNFUNDED_PAYMENT")
    ).verify(signed_payload(wallet, accepted), accepted)
    assert simulation.is_valid is False
    assert "simulation_failed" in (simulation.invalid_reason or "")

    payload = signed_payload(wallet, accepted).model_copy(
        update={
            "accepted": accepted.model_copy(update={"amount": "2000"})
        }
    )
    mismatch = service(FakeXRPL(wallet)).verify(payload, accepted)
    assert mismatch.is_valid is False
    assert "accepted_requirements_mismatch" in (
        mismatch.invalid_reason or ""
    )


def test_authorization_flow_is_equivalent_when_explicit_or_omitted() -> None:
    wallet = Wallet.create()
    implicit = xrp_requirements()
    explicit = xrp_requirements(paymentFlow="authorization")

    explicit_accepted = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, explicit), implicit
    )
    implicit_accepted = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, implicit), explicit
    )

    assert explicit_accepted.is_valid is True
    assert implicit_accepted.is_valid is True


def test_facilitator_rejects_non_authorization_payment_flow() -> None:
    wallet = Wallet.create()
    upfront = xrp_requirements(paymentFlow="upfront")

    result = service(FakeXRPL(wallet)).verify(
        signed_payload(wallet, upfront), upfront
    )

    assert result.is_valid is False
    assert result.invalid_reason == "invalid_exact_xrpl_requirements"
    assert "authorization" in (result.invalid_message or "")


def test_custom_network_requires_network_id() -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements().model_copy(
        update={"network": "xrpl:2048"}
    )
    custom = service(FakeXRPL(wallet), NETWORK_ID="xrpl:2048")
    valid = custom.verify(
        signed_payload(wallet, requirements, network_id=2048), requirements
    )
    missing = custom.verify(
        signed_payload(wallet, requirements), requirements
    )
    assert valid.is_valid is True
    assert missing.is_valid is False
    assert "network_id_mismatch" in (missing.invalid_reason or "")


def test_settlement_reserves_hash_and_never_rebroadcasts_pending(monkeypatch) -> None:
    monkeypatch.setattr("xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None)
    wallet = Wallet.create()
    requirements = xrp_requirements()
    payload = signed_payload(wallet, requirements)
    rpc = FakeXRPL(wallet)
    mechanism = service(rpc)

    first = mechanism.settle(payload, requirements)
    second = mechanism.settle(payload, requirements)

    assert first.error_reason == "settlement_pending"
    assert first.transaction
    assert second.error_reason == "settlement_pending"
    assert second.transaction == first.transaction
    assert rpc.submit_count == 1


def test_two_facilitator_instances_submit_only_once(monkeypatch) -> None:
    monkeypatch.setattr("xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None)
    wallet = Wallet.create()
    accepted = xrp_requirements()
    payload = signed_payload(wallet, accepted)
    shared_store = InMemorySettlementStore()
    rpc = FakeXRPL(wallet)
    first = service(rpc, store=shared_store)
    second = service(rpc, store=shared_store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda mechanism: mechanism.settle(payload, accepted),
                (first, second),
            )
        )

    assert all(result.error_reason == "settlement_pending" for result in results)
    assert results[0].transaction == results[1].transaction
    assert rpc.submit_count == 1


def test_indeterminate_submission_stays_reserved_until_ledger_expiry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )

    class IndeterminateSubmitXRPL(FakeXRPL):
        def request(self, request: Any) -> SimpleNamespace:
            if isinstance(request, SubmitOnly):
                self.submit_count += 1
                raise TimeoutError("submit confirmation timed out")
            if isinstance(request, Tx):
                assert request.min_ledger == 1000
                assert request.max_ledger == 1014
                return SimpleNamespace(
                    result={"error": "txnNotFound", "searched_all": True}
                )
            return super().request(request)

    wallet = Wallet.create()
    accepted = xrp_requirements()
    payload = signed_payload(wallet, accepted)
    rpc = IndeterminateSubmitXRPL(wallet)
    mechanism = service(rpc)

    first = mechanism.settle(payload, accepted)
    assert first.error_reason == "settlement_pending"
    assert first.transaction
    assert rpc.submit_count == 1

    monkeypatch.setitem(FakeXRPL.request.__globals__, "CURRENT_LEDGER", 1014)
    expired = mechanism.settle(payload, accepted)
    again = mechanism.settle(payload, accepted)
    assert expired.error_reason == "transaction_expired"
    assert again.error_reason == "transaction_expired"
    assert rpc.submit_count == 1


@pytest.mark.parametrize(
    "submit_response",
    [
        {},
        {"status": "error", "error": "upstream unavailable"},
        "malformed",
    ],
)
def test_ambiguous_submission_response_stays_pending_without_rebroadcast(
    monkeypatch,
    submit_response: Any,
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )

    class AmbiguousSubmitXRPL(FakeXRPL):
        def request(self, request: Any) -> SimpleNamespace:
            if isinstance(request, SubmitOnly):
                self.submit_count += 1
                return SimpleNamespace(result=submit_response)
            return super().request(request)

    wallet = Wallet.create()
    accepted = xrp_requirements()
    payload = signed_payload(wallet, accepted)
    rpc = AmbiguousSubmitXRPL(wallet)
    mechanism = service(rpc)

    first = mechanism.settle(payload, accepted)
    second = mechanism.settle(payload, accepted)

    assert first.error_reason == "settlement_pending"
    assert first.transaction
    assert second.error_reason == "settlement_pending"
    assert second.transaction == first.transaction
    assert rpc.submit_count == 1


@pytest.mark.parametrize("engine_result", ["terQUEUED", "telINSUF_FEE_P"])
def test_retryable_submission_result_stays_pending_without_rebroadcast(
    monkeypatch,
    engine_result: str,
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )
    wallet = Wallet.create()
    accepted = xrp_requirements()
    payload = signed_payload(wallet, accepted)
    rpc = FakeXRPL(wallet, submit_result=engine_result)
    mechanism = service(rpc)

    first = mechanism.settle(payload, accepted)
    second = mechanism.settle(payload, accepted)

    assert first.error_reason == "settlement_pending"
    assert second.transaction == first.transaction
    assert rpc.submit_count == 1


def test_validated_tessuccess_is_the_only_success(monkeypatch) -> None:
    monkeypatch.setattr("xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None)
    wallet = Wallet.create()
    requirements = xrp_requirements()
    rpc = FakeXRPL(
        wallet,
        tx_result={
            "validated": True,
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "delivered_amount": "1000",
            },
        },
    )
    result = service(rpc).settle(
        signed_payload(wallet, requirements), requirements
    )
    assert result.success is True
    assert result.extra == {"status": "validated"}


@pytest.mark.parametrize(
    ("delivered_amount", "reason"),
    [
        (None, "delivered_amount_unavailable"),
        ("999", "delivered_amount_mismatch"),
    ],
)
def test_validated_xrp_rejects_missing_or_mismatched_delivered_amount(
    monkeypatch,
    delivered_amount: Any,
    reason: str,
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )
    wallet = Wallet.create()
    accepted = xrp_requirements()
    meta: dict[str, Any] = {"TransactionResult": "tesSUCCESS"}
    if delivered_amount is not None:
        meta["delivered_amount"] = delivered_amount
    rpc = FakeXRPL(
        wallet,
        tx_result={"validated": True, "meta": meta},
    )

    result = service(rpc).settle(signed_payload(wallet, accepted), accepted)

    assert result.success is False
    assert reason in (result.error_reason or "")
    assert result.transaction


@pytest.mark.parametrize("issuer_matches", [True, False])
def test_validated_iou_checks_hex_currency_issuer_and_amount(
    monkeypatch,
    issuer_matches: bool,
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )
    wallet = Wallet.create()
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.5",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    rpc = FakeXRPL(
        wallet,
        tx_result={
            "validated": True,
            "meta": {
                "TransactionResult": "tesSUCCESS",
                "delivered_amount": {
                    "currency": RLUSD_HEX,
                    "issuer": (
                        RLUSD_TESTNET_ISSUER
                        if issuer_matches
                        else Wallet.create().classic_address
                    ),
                    "value": "0.5",
                },
            },
        },
    )

    result = service(rpc).settle(signed_payload(wallet, accepted), accepted)

    assert result.success is issuer_matches
    if not issuer_matches:
        assert "delivered_amount_mismatch" in (result.error_reason or "")


def test_preliminary_submission_failure_stays_pending(monkeypatch) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )
    wallet = Wallet.create()
    requirements = xrp_requirements()
    rpc = FakeXRPL(wallet, submit_result="tecUNFUNDED_PAYMENT")
    mechanism = service(rpc)
    payload = signed_payload(wallet, requirements)
    result = mechanism.settle(payload, requirements)
    retry = mechanism.settle(payload, requirements)
    assert result.success is False
    assert result.error_reason == "settlement_pending"
    assert result.transaction
    assert retry.error_reason == "settlement_pending"
    assert retry.transaction == result.transaction
    assert rpc.submit_count == 1


def test_validated_submission_failure_is_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )
    wallet = Wallet.create()
    requirements = xrp_requirements()
    rpc = FakeXRPL(
        wallet,
        submit_result="tecUNFUNDED_PAYMENT",
        tx_result={
            "validated": True,
            "meta": {"TransactionResult": "tecUNFUNDED_PAYMENT"},
        },
    )
    mechanism = service(rpc)
    payload = signed_payload(wallet, requirements)

    result = mechanism.settle(payload, requirements)
    retry = mechanism.settle(payload, requirements)

    assert result.success is False
    assert result.error_reason == (
        "transaction_failed: tecUNFUNDED_PAYMENT"
    )
    assert retry == result
    assert rpc.submit_count == 1


@pytest.mark.parametrize(
    "lookup_result",
    [
        RuntimeError("tx rpc unavailable"),
        {"error": "txnNotFound", "searched_all": False},
        {"error": "txnNotFound"},
        {"error": "internal", "searched_all": True},
    ],
)
def test_uncertain_transaction_lookup_never_expires_or_rebroadcasts(
    monkeypatch,
    lookup_result: Exception | dict[str, Any],
) -> None:
    monkeypatch.setattr(
        "xrpl_x402_facilitator.xrpl_service.time.sleep", lambda _: None
    )

    class UncertainLookupXRPL(FakeXRPL):
        def __init__(self, wallet: Wallet) -> None:
            super().__init__(wallet)
            self.tx_requests: list[Tx] = []

        def request(self, request: Any) -> SimpleNamespace:
            if isinstance(request, Tx):
                self.tx_requests.append(request)
                if isinstance(lookup_result, Exception):
                    raise lookup_result
                return SimpleNamespace(result=lookup_result)
            return super().request(request)

    wallet = Wallet.create()
    requirements = xrp_requirements()
    rpc = UncertainLookupXRPL(wallet)
    mechanism = service(rpc)
    payload = signed_payload(wallet, requirements)

    first = mechanism.settle(payload, requirements)
    monkeypatch.setitem(FakeXRPL.request.__globals__, "CURRENT_LEDGER", 1014)
    retry = mechanism.settle(payload, requirements)

    assert first.error_reason == "settlement_pending"
    assert retry.error_reason == "settlement_pending"
    assert retry.transaction == first.transaction
    assert rpc.submit_count == 1
    assert len(rpc.tx_requests) == 2
    assert all(request.min_ledger == 1000 for request in rpc.tx_requests)
    assert all(request.max_ledger == 1014 for request in rpc.tx_requests)


def test_rpc_outage_is_an_infrastructure_failure() -> None:
    class BrokenXRPL:
        def request(self, request: Any) -> None:
            raise RuntimeError("rpc unavailable")

    wallet = Wallet.create()
    accepted = xrp_requirements()
    mechanism = ExactXRPLFacilitatorScheme(
        settings(),
        client=BrokenXRPL(),
        settlement_store=InMemorySettlementStore(),
    )
    with pytest.raises(RuntimeError, match="rpc unavailable"):
        mechanism.verify(signed_payload(wallet, accepted), accepted)


def test_client_signer_matches_typescript_wire_shape() -> None:
    wallet = Wallet.create()
    requirements = xrp_requirements(invoiceId="cross-sdk")
    signer = XRPLPaymentSigner(
        wallet,
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=1014,
    )
    inner = signer.build_x402_payload(requirements)
    assert set(inner) == {"signedTxBlob"}
    decoded = binarycodec.decode(inner["signedTxBlob"])
    assert decoded["Amount"] == "1000"
    assert decoded["InvoiceID"] == invoice_id_to_invoice_id_field("cross-sdk")
    assert decoded["Sequence"] == 7
    assert decoded["LastLedgerSequence"] == 1014


def test_client_signer_builds_ticket_flow_and_caps_inventory_target() -> None:
    wallet = Wallet.create()
    accepted = xrp_requirements(assetTransferMethod="ticketSequence")
    signer = XRPLPaymentSigner(
        wallet,
        network="xrpl:1",
        autofill_enabled=False,
        default_last_ledger_sequence=1014,
        get_available_ticket_sequence=lambda _account, _network: 55,
    )
    decoded = binarycodec.decode(signer.sign_requirements(accepted))
    assert decoded["Sequence"] == 0
    assert decoded["TicketSequence"] == 55

    with pytest.raises(ValueError, match="ticket_inventory_target"):
        XRPLPaymentSigner(wallet, ticket_inventory_target=251)


def test_client_ticket_inventory_paginates_account_objects() -> None:
    class PaginatedTicketClient:
        def __init__(self) -> None:
            self.markers: list[Any] = []

        def request(self, request: Any) -> SimpleNamespace:
            assert isinstance(request, AccountObjects)
            self.markers.append(request.marker)
            if request.marker is None:
                return SimpleNamespace(
                    result={"account_objects": [], "marker": "next"}
                )
            return SimpleNamespace(
                result={"account_objects": [{"TicketSequence": 55}]}
            )

    wallet = Wallet.create()
    accepted = xrp_requirements(assetTransferMethod="ticketSequence")
    rpc = PaginatedTicketClient()
    signer = XRPLPaymentSigner(
        wallet,
        network="xrpl:1",
        client=rpc,
        autofill_enabled=False,
        default_last_ledger_sequence=1014,
        ticket_inventory_target=0,
    )

    decoded = binarycodec.decode(signer.sign_requirements(accepted))

    assert decoded["TicketSequence"] == 55
    assert rpc.markers == [None, "next"]


def test_official_typescript_payload_fixture_verifies_in_python(monkeypatch) -> None:
    fixture = json.loads(
        Path("tests/fixtures/cross-sdk/typescript-exact-xrpl.json").read_text()
    )
    accepted = PaymentRequirements.model_validate(fixture["paymentRequirements"])
    payload = PaymentPayload.model_validate(fixture["paymentPayload"])
    # Public upstream cross-SDK test-vector seed. Never fund or reuse it.
    payer = Wallet.from_seed("sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
    python_blob = XRPLPaymentSigner(
        payer,
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=1,
        default_fee="12",
        default_last_ledger_sequence=1000,
    ).build_x402_payload(accepted)["signedTxBlob"]
    assert python_blob == fixture["paymentPayload"]["payload"]["signedTxBlob"]
    monkeypatch.setitem(settings.__globals__, "DESTINATION", accepted.pay_to)
    monkeypatch.setitem(FakeXRPL.request.__globals__, "CURRENT_LEDGER", 990)
    mechanism = ExactXRPLFacilitatorScheme(
        settings(),
        client=FakeXRPL(payer, sequence=1),
        settlement_store=InMemorySettlementStore(),
    )
    result = mechanism.verify(payload, accepted)
    assert result.is_valid is True
    assert result.payer == fixture["payer"]

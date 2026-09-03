from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from x402.schemas import PaymentPayload, PaymentRequirements
from xrpl.core import binarycodec
from xrpl.core.keypairs import derive_classic_address
from xrpl.models.requests import AccountInfo, AccountObjects, Ledger, Simulate
from xrpl.wallet import Wallet

from xrpl_x402_client import XRPLPaymentSigner
from xrpl_x402_facilitator.config import Settings
from xrpl_x402_facilitator.replay_store import InMemorySettlementStore
from xrpl_x402_facilitator.xrpl_service import ExactXRPLFacilitatorScheme


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "cross-sdk"
    / "typescript-exact-xrpl-extended.json"
)
FIXTURE = json.loads(FIXTURE_PATH.read_text())
VECTORS = FIXTURE["vectors"]
LSF_DISABLE_MASTER = 0x00100000


class FixtureXRPL:
    def __init__(self, ledger_state: dict[str, Any]) -> None:
        self._state = ledger_state

    def request(self, request: Any) -> SimpleNamespace:
        if isinstance(request, Ledger):
            return SimpleNamespace(
                result={
                    "ledger_index": self._state["currentLedgerIndex"]
                }
            )
        if isinstance(request, AccountInfo):
            account_data: dict[str, Any] = {
                "Sequence": self._state["accountSequence"],
                "Balance": "100000000",
                "Flags": (
                    LSF_DISABLE_MASTER
                    if self._state["masterKeyDisabled"]
                    else 0
                ),
            }
            if self._state["regularKey"] is not None:
                account_data["RegularKey"] = self._state["regularKey"]
            return SimpleNamespace(result={"account_data": account_data})
        if isinstance(request, AccountObjects):
            ticket = self._state["ticketSequence"]
            return SimpleNamespace(
                result={
                    "account_objects": (
                        [{"TicketSequence": ticket}]
                        if ticket is not None
                        else []
                    )
                }
            )
        if isinstance(request, Simulate):
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})
        raise AssertionError(f"unexpected fixture RPC request: {type(request)}")


def _wallets(vector: dict[str, Any]) -> tuple[Wallet, Wallet]:
    wallet_fixture = vector["walletFixture"]
    account_wallet = Wallet.from_entropy(
        wallet_fixture["accountEntropyHex"]
    )
    signing_entropy = wallet_fixture.get("signingEntropyHex")
    if signing_entropy is None:
        return account_wallet, account_wallet
    signing_wallet = Wallet.from_entropy(
        signing_entropy,
        master_address=account_wallet.classic_address,
    )
    return account_wallet, signing_wallet


def _settings(requirements: PaymentRequirements) -> Settings:
    return Settings(
        _env_file=None,
        MY_DESTINATION_ADDRESS=requirements.pay_to,
        FACILITATOR_BEARER_TOKEN="cross-sdk-test-token",
        REDIS_URL="redis://unused:6379/0",
        NETWORK_ID=str(requirements.network),
        VALIDATION_TIMEOUT=1,
    )


def test_fixture_records_pinned_official_typescript_provenance() -> None:
    assert FIXTURE["source"] == {
        "repository": "x402-foundation/x402",
        "commit": "f8a3682de3e65d18670bd837212ed49985094e13",
        "package": "@x402/xrpl@2.23.0",
        "packageIntegrity": (
            "sha512-mdb/YoSTBwXZFuCJrMoWtzryXYzkAWgQnKfrQdhDc1fdgX"
            "01d2e0NAH9t5RGOETu5RNyeD0saHEnBq3Ob8fTvg=="
        ),
        "runtime": "xrpl@4.6.0",
        "sourceFile": (
            "typescript/packages/mechanisms/xrpl/src/exact/client/scheme.ts"
        ),
        "generatorApi": (
            "ExactXrplScheme.createPaymentPayload with createXrplWalletSigner"
        ),
        "validatorApi": (
            "ExactXrplScheme.verify from @x402/xrpl/exact/facilitator"
        ),
        "envelopeNote": (
            "The x402 client adds accepted to the scheme result to form "
            "PaymentPayload."
        ),
        "warning": (
            "Deterministic test-only wallet entropy; never fund these "
            "accounts."
        ),
    }


@pytest.mark.parametrize("vector", VECTORS, ids=lambda item: item["name"])
def test_official_typescript_payload_verifies_in_python(
    vector: dict[str, Any],
) -> None:
    requirements = PaymentRequirements.model_validate(
        vector["paymentRequirements"]
    )
    payload = PaymentPayload.model_validate(vector["paymentPayload"])
    signed_blob = payload.payload["signedTxBlob"]
    decoded = binarycodec.decode(signed_blob)

    assert payload.accepted == requirements
    assert set(payload.payload) == {"signedTxBlob"}
    assert decoded == vector["expectedTransaction"]
    assert decoded["Account"] == vector["payer"]
    assert derive_classic_address(decoded["SigningPubKey"]) == vector[
        "signingKeyAddress"
    ]

    mechanism = ExactXRPLFacilitatorScheme(
        _settings(requirements),
        client=FixtureXRPL(vector["ledgerState"]),
        settlement_store=InMemorySettlementStore(),
    )
    result = mechanism.verify(payload, requirements)

    assert result.is_valid is True
    assert result.payer == vector["payer"]
    assert result.extra == {"verificationPath": "simulation"}


@pytest.mark.parametrize("vector", VECTORS, ids=lambda item: item["name"])
def test_python_signer_matches_official_typescript_wire_vector(
    vector: dict[str, Any],
) -> None:
    requirements = PaymentRequirements.model_validate(
        vector["paymentRequirements"]
    )
    account_wallet, signing_wallet = _wallets(vector)
    options = vector["clientOptions"]
    ticket = options["ticketSequence"]
    signer = XRPLPaymentSigner(
        signing_wallet,
        network=str(requirements.network),
        autofill_enabled=False,
        default_fee=options["feeDrops"],
        default_sequence=options["accountSequence"],
        default_last_ledger_sequence=vector["expectedTransaction"][
            "LastLedgerSequence"
        ],
        get_available_ticket_sequence=(
            (lambda _account, _network: int(ticket))
            if ticket is not None
            else None
        ),
    )

    assert signing_wallet.classic_address == account_wallet.classic_address
    python_payload = signer.build_x402_payload(requirements)
    assert python_payload == vector["paymentPayload"]["payload"]
    assert binarycodec.decode(python_payload["signedTxBlob"]) == vector[
        "expectedTransaction"
    ]

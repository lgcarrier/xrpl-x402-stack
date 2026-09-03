from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from x402 import x402Client
from x402.extensions.payment_identifier import (
    declare_payment_identifier_extension,
    extract_payment_identifier,
)
from x402.http import (
    decode_payment_required_header,
    decode_payment_response_header,
    decode_payment_signature_header,
    encode_payment_required_header,
    encode_payment_response_header,
    encode_payment_signature_header,
)
from x402.schemas import (
    AssetAmount,
    NoMatchingRequirementsError,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
    VerifyResponse,
)
from xrpl.wallet import Wallet

from xrpl_x402_client import (
    XRPLPaymentSigner,
    register_exact_xrpl_client,
    select_payment_option,
)
from xrpl_x402_core import ExactXRPLPayload, RLUSD_HEX, RLUSD_TESTNET_ISSUER
from xrpl_x402_middleware import ExactXRPLServerScheme


def requirements() -> PaymentRequirements:
    return PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset=RLUSD_HEX,
        amount="0.25",
        pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
        max_timeout_seconds=60,
        extra={
            "issuer": RLUSD_TESTNET_ISSUER,
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
            "invoiceId": "order-42",
        },
    )


def test_canonical_v2_json_and_headers_round_trip_exactly() -> None:
    accepted = requirements()
    resource = ResourceInfo(
        url="https://merchant.example/premium",
        description="Premium data",
        mime_type="application/json",
        service_name="Example Merchant",
        tags=["data", "xrpl"],
        icon_url="https://merchant.example/icon.png",
    )
    extension = {"future-extension": {"info": {"opaque": True}}}
    required = PaymentRequired(
        x402_version=2,
        resource=resource,
        accepts=[accepted],
        extensions=extension,
    )
    payload = PaymentPayload(
        x402_version=2,
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        resource=resource,
        extensions=extension,
    )
    settled = SettleResponse(
        success=True,
        payer="rPayer",
        transaction="A" * 64,
        network="xrpl:1",
        amount="0.25",
        extensions=extension,
    )

    assert decode_payment_required_header(
        encode_payment_required_header(required)
    ) == required
    assert decode_payment_signature_header(
        encode_payment_signature_header(payload)
    ) == payload
    assert decode_payment_response_header(
        encode_payment_response_header(settled)
    ) == settled

    raw = json.loads(
        base64.b64decode(encode_payment_signature_header(payload))
    )
    assert raw["x402Version"] == 2
    assert raw["accepted"]["amount"] == "0.25"
    assert raw["payload"] == {"signedTxBlob": "AA"}
    assert raw["extensions"] == extension


def test_xrpl_payload_uses_camel_case_and_rejects_legacy_fields() -> None:
    assert ExactXRPLPayload(signedTxBlob="aa").model_dump(by_alias=True) == {
        "signedTxBlob": "AA"
    }
    with pytest.raises(ValidationError):
        ExactXRPLPayload.model_validate({"signed_tx_blob": "AA"})


def test_official_payment_payload_requires_accepted_requirements() -> None:
    with pytest.raises(ValidationError):
        PaymentPayload.model_validate(
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "xrpl:1",
                "payload": {"signedTxBlob": "AA"},
            }
        )


@pytest.mark.parametrize("asset,amount,extra", [
    ("XRP", "0", {"areFeesSponsored": False}),
    (RLUSD_HEX, "0.0", {"areFeesSponsored": False, "issuer": RLUSD_TESTNET_ISSUER}),
])
def test_exact_requirements_reject_zero_amount(asset, amount, extra) -> None:  # type: ignore[no-untyped-def]
    from xrpl_x402_core import validate_requirements_shape

    with pytest.raises(ValueError, match="greater than zero"):
        validate_requirements_shape(
            PaymentRequirements(
                scheme="exact",
                network="xrpl:1",
                asset=asset,
                amount=amount,
                pay_to="rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
                max_timeout_seconds=60,
                extra=extra,
            )
        )


def test_default_iou_requires_the_official_network_issuer() -> None:
    from xrpl_x402_core import validate_requirements_shape

    with pytest.raises(ValueError, match="payments require extra.issuer"):
        validate_requirements_shape(
            requirements().model_copy(
                update={
                    "extra": {
                        **(requirements().extra or {}),
                        "issuer": "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
                    }
                }
            )
        )


@pytest.mark.parametrize(
    "asset",
    ["RLUSD", RLUSD_HEX.lower(), "usd"],
)
def test_requirements_reject_noncanonical_wire_assets(asset: str) -> None:
    from xrpl_x402_core import validate_requirements_shape

    with pytest.raises(ValueError, match="XRPL wire assets must use"):
        validate_requirements_shape(
            requirements().model_copy(update={"asset": asset})
        )


def test_server_canonicalizes_configured_iou_symbols_to_wire_codes() -> None:
    parsed = ExactXRPLServerScheme().parse_price(
        AssetAmount(
            asset="RLUSD",
            amount="0.25",
            extra={"issuer": RLUSD_TESTNET_ISSUER},
        ),
        "xrpl:1",
    )

    assert parsed.asset == RLUSD_HEX


def test_registered_client_populates_required_payment_identifier() -> None:
    required = PaymentRequired(
        resource=ResourceInfo(url="https://merchant.example/unsafe"),
        accepts=[requirements()],
        extensions={
            "payment-identifier": declare_payment_identifier_extension(
                required=True
            )
        },
    )
    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=1014,
    )
    client = register_exact_xrpl_client(
        x402Client(), signer, networks="xrpl:1"
    )
    client.set_spend_controls(False)

    payload = asyncio.run(client.create_payment_payload(required))

    assert extract_payment_identifier(payload)
    assert payload.extensions != required.extensions


def test_xrpl_clients_select_authorization_from_mixed_flows_and_networks() -> None:
    authorization = requirements().model_copy(
        update={
            "extra": {
                **(requirements().extra or {}),
                "paymentFlow": "authorization",
            }
        }
    )
    upfront = requirements().model_copy(
        update={
            "extra": {
                **(requirements().extra or {}),
                "paymentFlow": "upfront",
            }
        }
    )
    non_xrpl = requirements().model_copy(
        update={"network": "eip155:8453"}
    )
    challenge = PaymentRequired(
        resource=ResourceInfo(url="https://merchant.example/mixed"),
        accepts=[non_xrpl, upfront, authorization],
    )

    assert select_payment_option(challenge) == authorization

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=1014,
    )
    client = register_exact_xrpl_client(
        x402Client(), signer, networks="xrpl:1"
    )
    client.set_spend_controls(False)

    payload = asyncio.run(client.create_payment_payload(challenge))

    assert payload.accepted == authorization


def test_xrpl_clients_reject_upfront_only_requirements() -> None:
    upfront = requirements().model_copy(
        update={
            "extra": {
                **(requirements().extra or {}),
                "paymentFlow": "upfront",
            }
        }
    )
    challenge = PaymentRequired(
        resource=ResourceInfo(url="https://merchant.example/upfront"),
        accepts=[upfront],
    )

    with pytest.raises(ValueError, match="authorization"):
        select_payment_option(challenge)

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
        default_sequence=7,
        default_last_ledger_sequence=1014,
    )
    client = register_exact_xrpl_client(
        x402Client(), signer, networks="xrpl:1"
    )
    client.set_spend_controls(False)

    with pytest.raises(
        NoMatchingRequirementsError, match="filtered out by policies"
    ):
        asyncio.run(client.create_payment_payload(challenge))


def test_committed_v2_fixtures_match_official_models_and_header_codecs() -> None:
    fixture = json.loads(
        Path("tests/fixtures/x402-v2/canonical-messages.json").read_text()
    )
    required = PaymentRequired.model_validate(fixture["paymentRequired"])
    payload = PaymentPayload.model_validate(fixture["paymentPayload"])
    verify = VerifyResponse.model_validate(fixture["verifyResponse"])
    settled = SettleResponse.model_validate(fixture["settleResponse"])

    assert required.model_dump(mode="json", by_alias=True, exclude_none=True) == (
        fixture["paymentRequired"]
    )
    assert payload.model_dump(mode="json", by_alias=True, exclude_none=True) == (
        fixture["paymentPayload"]
    )
    assert verify.model_dump(mode="json", by_alias=True, exclude_none=True) == (
        fixture["verifyResponse"]
    )
    assert settled.model_dump(mode="json", by_alias=True, exclude_none=True) == (
        fixture["settleResponse"]
    )
    assert encode_payment_required_header(required) == fixture["headers"]["PAYMENT-REQUIRED"]
    assert encode_payment_signature_header(payload) == fixture["headers"]["PAYMENT-SIGNATURE"]
    assert encode_payment_response_header(settled) == fixture["headers"]["PAYMENT-RESPONSE"]

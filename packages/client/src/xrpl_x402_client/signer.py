from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable
from uuid import uuid4

from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_payment_required_header,
    encode_payment_signature_header,
)
from x402.extensions.payment_identifier import (
    append_payment_identifier_to_extensions,
)
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements
from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountObjects, Ledger
from xrpl.models.transactions import Payment, TicketCreate
from xrpl.transaction import autofill, sign, submit_and_wait
from xrpl.wallet import Wallet

from xrpl_x402_core import (
    ExactXRPLPayload,
    find_default_asset,
    get_max_last_ledger_sequence,
    invoice_id_to_invoice_id_field,
    is_valid_xrpl_network,
    normalize_currency_code,
    parse_xrpl_network_id,
    validate_requirements_shape,
    xrpl_currency_code,
)


class XRPLPaymentSigner:
    """Construct and sign complete XRPL Payment transactions for x402."""

    def __init__(
        self,
        wallet: Wallet,
        *,
        rpc_url: str = "https://s1.ripple.com:51234",
        network: str | None = None,
        client: JsonRpcClient | None = None,
        autofill_enabled: bool = True,
        default_fee: str = "12",
        default_sequence: int = 1,
        default_last_ledger_sequence: int | None = None,
        ticket_inventory_target: int = 0,
        get_current_ledger_index: Callable[[str], int] | None = None,
        get_available_ticket_sequence: Callable[[str, str], int | None] | None = None,
    ) -> None:
        if not 0 <= ticket_inventory_target <= 250:
            raise ValueError(
                "ticket_inventory_target must be between 0 and 250"
            )
        self.wallet = wallet
        self.network = network
        self._client = client or JsonRpcClient(rpc_url)
        self._autofill_enabled = autofill_enabled
        self._default_fee = default_fee
        self._default_sequence = default_sequence
        self._default_last_ledger_sequence = default_last_ledger_sequence
        self._ticket_inventory_target = ticket_inventory_target
        self._get_current_ledger_index = get_current_ledger_index
        self._get_available_ticket_sequence = get_available_ticket_sequence

    @property
    def classic_address(self) -> str:
        return self.wallet.classic_address

    def build_x402_payload(self, requirements: PaymentRequirements) -> dict[str, str]:
        return ExactXRPLPayload(
            signedTxBlob=self.sign_requirements(requirements)
        ).model_dump(by_alias=True)

    def sign_requirements(self, requirements: PaymentRequirements) -> str:
        extra = validate_requirements_shape(requirements)
        if self.network is not None and str(requirements.network) != self.network:
            raise ValueError(
                f"Payment requirements network {requirements.network} does not match signer network {self.network}"
            )
        method = extra.asset_transfer_method or "sequence"
        network_id = parse_xrpl_network_id(str(requirements.network))
        current_ledger = self._current_ledger(str(requirements.network))
        last_ledger_sequence = (
            get_max_last_ledger_sequence(current_ledger, requirements)
            if current_ledger is not None
            else self._default_last_ledger_sequence
        )

        amount: str | IssuedCurrencyAmount = requirements.amount
        send_max: IssuedCurrencyAmount | None = None
        if requirements.asset != "XRP":
            issued = IssuedCurrencyAmount(
                currency=xrpl_currency_code(requirements.asset),
                issuer=extra.issuer or "",
                value=requirements.amount,
            )
            amount = issued
            send_max = issued

        payment_kwargs: dict[str, Any] = {
            "account": self.wallet.classic_address,
            "destination": requirements.pay_to,
            "amount": amount,
        }
        if send_max is not None:
            payment_kwargs["send_max"] = send_max
        if extra.invoice_id is not None:
            payment_kwargs["invoice_id"] = invoice_id_to_invoice_id_field(
                extra.invoice_id
            )
        if extra.destination_tag is not None:
            payment_kwargs["destination_tag"] = extra.destination_tag
        if network_id > 1024:
            payment_kwargs["network_id"] = network_id
        if last_ledger_sequence is not None:
            payment_kwargs["last_ledger_sequence"] = last_ledger_sequence
        if method == "ticketSequence":
            payment_kwargs["sequence"] = 0
            payment_kwargs["ticket_sequence"] = self._available_ticket(
                str(requirements.network)
            )
        elif not self._autofill_enabled:
            payment_kwargs["sequence"] = self._default_sequence
        if not self._autofill_enabled:
            payment_kwargs["fee"] = self._default_fee

        payment = Payment(**payment_kwargs)
        if self._autofill_enabled:
            payment = autofill(payment, self._client)
        self._validate_prepared(payment, method, network_id)
        return sign(payment, self.wallet).blob().upper()

    def _current_ledger(self, network: str) -> int | None:
        if self._get_current_ledger_index is not None:
            return int(self._get_current_ledger_index(network))
        if not self._autofill_enabled:
            return None
        response = self._client.request(Ledger(ledger_index="validated"))
        return int(response.result["ledger_index"])

    def _available_ticket(self, network: str) -> int:
        if self._get_available_ticket_sequence is not None:
            ticket = self._get_available_ticket_sequence(
                self.wallet.classic_address, network
            )
            if ticket is not None:
                return int(ticket)
            tickets: list[int] = []
        else:
            tickets = self._available_ticket_sequences()
        missing = self._ticket_inventory_target - len(tickets)
        if missing > 0:
            response = submit_and_wait(
                TicketCreate(
                    account=self.wallet.classic_address,
                    ticket_count=missing,
                ),
                self._client,
                self.wallet,
            )
            tickets.extend(
                _ticket_sequences_from_metadata(response.result.get("meta"))
            )
        if tickets:
            return min(tickets)
        if self._ticket_inventory_target == 0:
            raise ValueError(
                "No available XRPL ticket; automatic ticket creation is disabled"
            )
        raise ValueError("TicketCreate returned no tickets")

    def _available_ticket_sequences(self) -> list[int]:
        tickets: list[int] = []
        marker: Any | None = None
        seen_markers: set[str] = set()
        while True:
            response = self._client.request(
                AccountObjects(
                    account=self.wallet.classic_address,
                    ledger_index="validated",
                    type="ticket",
                    limit=400,
                    marker=marker,
                )
            )
            result = response.result
            if not isinstance(result, dict):
                raise ValueError("AccountObjects returned a malformed result")
            objects = result.get("account_objects", [])
            if not isinstance(objects, list):
                raise ValueError(
                    "AccountObjects returned malformed account_objects"
                )
            tickets.extend(
                int(obj["TicketSequence"])
                for obj in objects
                if isinstance(obj, dict) and "TicketSequence" in obj
            )
            marker = result.get("marker")
            if marker is None:
                return sorted(set(tickets))
            marker_key = repr(marker)
            if marker_key in seen_markers:
                raise ValueError(
                    "AccountObjects returned a repeated pagination marker"
                )
            seen_markers.add(marker_key)

    @staticmethod
    def _validate_prepared(
        payment: Payment, method: str, network_id: int
    ) -> None:
        if payment.fee is None or not str(payment.fee).isdigit():
            raise ValueError("Prepared payment must set Fee in drops")
        if payment.last_ledger_sequence is None:
            raise ValueError("Prepared payment must set LastLedgerSequence")
        if method == "sequence":
            if payment.ticket_sequence is not None:
                raise ValueError("sequence payments must not set TicketSequence")
            if payment.sequence in {None, 0}:
                raise ValueError("sequence payments must set the account Sequence")
        else:
            if payment.sequence != 0 or payment.ticket_sequence is None:
                raise ValueError(
                    "ticketSequence payments require Sequence 0 and TicketSequence"
                )
        if network_id <= 1024 and payment.network_id is not None:
            raise ValueError("Standard XRPL networks must omit NetworkID")
        if network_id > 1024 and payment.network_id != network_id:
            raise ValueError("Custom XRPL networks must set the matching NetworkID")


class ExactXRPLClientScheme:
    scheme = "exact"

    @staticmethod
    def find_default_asset(asset: str, network: str) -> dict[str, object] | None:
        default = find_default_asset(asset, network)
        if default is None:
            return None
        # XRPL IOU amounts are ledger decimal values, not atomic integers.
        # The upstream spend-control engine uses decimals only when an amount
        # contains no decimal point, so zero preserves the XRPL value semantics.
        return {**default, "decimals": 0}

    def __init__(self, signer: XRPLPaymentSigner) -> None:
        self._signer = signer

    def create_payment_payload(
        self,
        requirements: PaymentRequirements,
        extensions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = self._signer.build_x402_payload(requirements)
        if extensions:
            enriched = deepcopy(extensions)
            append_payment_identifier_to_extensions(enriched)
            if enriched != extensions:
                result["__extensions"] = enriched
        return result


def register_exact_xrpl_client(
    client: Any,
    signer: XRPLPaymentSigner,
    networks: str | list[str] | None = None,
) -> Any:
    scheme = ExactXRPLClientScheme(signer)
    selected = (
        [networks]
        if isinstance(networks, str)
        else networks or ["xrpl:0", "xrpl:1", "xrpl:2"]
    )
    for network in selected:
        client.register(network, scheme)
    client.register_policy(_enforce_default_asset_issuers)
    return client


def _enforce_default_asset_issuers(
    _version: int,
    requirements: list[PaymentRequirements],
) -> list[PaymentRequirements]:
    accepted: list[PaymentRequirements] = []
    for requirement in requirements:
        is_exact_xrpl = (
            requirement.scheme == "exact"
            and is_valid_xrpl_network(str(requirement.network))
        )
        if is_exact_xrpl and not _is_authorization_flow(requirement):
            continue
        if not is_exact_xrpl:
            accepted.append(requirement)
            continue
        default = find_default_asset(
            requirement.asset, str(requirement.network)
        )
        if default is None:
            accepted.append(requirement)
            continue
        if (requirement.extra or {}).get("issuer") == default.get("issuer"):
            accepted.append(requirement)
    return accepted


def _is_authorization_flow(requirement: PaymentRequirements) -> bool:
    return (requirement.extra or {}).get("paymentFlow") in {
        None,
        "authorization",
    }


def select_payment_option(
    payment_required: PaymentRequired,
    *,
    network: str | None = None,
    asset: str | None = None,
    issuer: str | None = None,
) -> PaymentRequirements:
    candidates = [
        item
        for item in payment_required.accepts
        if item.scheme == "exact"
        and is_valid_xrpl_network(str(item.network))
        and _is_authorization_flow(item)
    ]
    if network is not None:
        candidates = [
            item for item in candidates if str(item.network) == network
        ]
    if asset is not None:
        wanted_asset = normalize_currency_code(asset)
        candidates = [
            item
            for item in candidates
            if normalize_currency_code(item.asset) == wanted_asset
        ]
    if issuer is not None:
        candidates = [
            item
            for item in candidates
            if (item.extra or {}).get("issuer") == issuer
        ]
    if not candidates:
        raise ValueError(
            "No matching XRPL authorization payment requirements found"
        )
    return candidates[0]


def build_payment_signature(
    payment_required: PaymentRequired | PaymentRequirements,
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    asset: str | None = None,
    issuer: str | None = None,
    payment_identifier: str | None = None,
) -> str:
    requirements = (
        select_payment_option(
            payment_required, network=network, asset=asset, issuer=issuer
        )
        if isinstance(payment_required, PaymentRequired)
        else payment_required
    )
    extensions = deepcopy(
        payment_required.extensions
        if isinstance(payment_required, PaymentRequired)
        else None
    )
    if extensions:
        append_payment_identifier_to_extensions(
            extensions,
            payment_identifier or f"xrpl-x402-{uuid4().hex}",
        )
    payload = PaymentPayload(
        x402_version=2,
        payload=ExactXRPLClientScheme(signer).create_payment_payload(requirements),
        accepted=requirements,
        resource=(
            payment_required.resource
            if isinstance(payment_required, PaymentRequired)
            else None
        ),
        extensions=extensions,
    )
    return encode_payment_signature_header(payload)


def decode_payment_required(raw_header: str) -> PaymentRequired:
    decoded = decode_payment_required_header(raw_header)
    if not isinstance(decoded, PaymentRequired) or decoded.x402_version != 2:
        raise ValueError(
            "Only canonical x402 v2 PAYMENT-REQUIRED headers are supported"
        )
    return decoded


def decode_payment_required_response(
    *, headers: dict[str, str], body: bytes | None = None
) -> PaymentRequired:
    del body
    raw = headers.get(PAYMENT_REQUIRED_HEADER) or headers.get(
        PAYMENT_REQUIRED_HEADER.lower()
    )
    if not raw:
        raise ValueError("402 response did not include PAYMENT-REQUIRED")
    return decode_payment_required(raw)


def _ticket_sequences_from_metadata(meta: Any) -> list[int]:
    if not isinstance(meta, dict):
        return []
    sequences: list[int] = []
    for node in meta.get("AffectedNodes", []):
        created = node.get("CreatedNode", {}) if isinstance(node, dict) else {}
        if created.get("LedgerEntryType") != "Ticket":
            continue
        fields = created.get("NewFields", {})
        if "TicketSequence" in fields:
            sequences.append(int(fields["TicketSequence"]))
    return sequences

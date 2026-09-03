from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from x402.schemas import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
)
from xrpl.clients import JsonRpcClient
from xrpl.core import binarycodec
from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.models.requests import (
    AccountInfo,
    AccountLines,
    AccountObjects,
    AccountObjectType,
    Ledger,
    Simulate,
    SubmitOnly,
    ServerState,
    Tx,
)
from xrpl.models.transactions import Payment

from xrpl_x402_core import (
    TF_PARTIAL_PAYMENT,
    XRPLSettlementState,
    compare_decimal_strings,
    exact_xrpl_payload,
    get_max_last_ledger_sequence,
    invoice_id_to_invoice_id_field,
    parse_xrpl_network_id,
    requirements_fingerprint,
    signed_transaction_hash,
    supported_asset_keys,
    validate_requirements_shape,
    xrpl_currency_code,
)
from xrpl_x402_facilitator.config import Settings, get_settings
from xrpl_x402_facilitator.replay_store import (
    SettlementStore,
    build_settlement_store,
)

logger = structlog.get_logger()

CANONICAL_SIGNING_PUB_KEY_PATTERN = re.compile(
    r"^(02|03|ED)[0-9A-F]{64}$", re.IGNORECASE
)
LSF_DISABLE_MASTER = 0x00100000
SETTLEMENT_MARGIN_SECONDS = 120
ALLOWED_PAYMENT_FIELDS = frozenset(
    {
        "TransactionType",
        "Account",
        "Destination",
        "DestinationTag",
        "Amount",
        "DeliverMax",
        "SendMax",
        "Fee",
        "Sequence",
        "TicketSequence",
        "LastLedgerSequence",
        "NetworkID",
        "InvoiceID",
        "Flags",
        "SigningPubKey",
        "TxnSignature",
    }
)

SIMULATION_UNAVAILABLE_ERRORS = frozenset(
    {
        "methodnotfound",
        "notimplemented",
        "notsupported",
        "unknowncmd",
        "unknowncommand",
        "unknownmethod",
    }
)


def _is_simulation_unavailable_result(result: dict[str, Any]) -> bool:
    error = result.get("error")
    error_code = result.get("error_code", result.get("code"))
    candidates = [result.get("error_message"), result.get("message")]
    if isinstance(error, dict):
        error_code = error.get("code", error_code)
        candidates.extend((error.get("name"), error.get("message")))
    else:
        candidates.append(error)

    if str(error_code) == "-32601":
        return True

    return any(
        re.sub(r"[^a-z0-9]+", "", candidate.lower())
        in SIMULATION_UNAVAILABLE_ERRORS
        for candidate in candidates
        if isinstance(candidate, str)
    )


@dataclass(frozen=True, slots=True)
class VerifiedTransaction:
    transaction: dict[str, Any]
    transaction_hash: str
    payer: str
    verification_path: str


@dataclass(frozen=True, slots=True)
class TransactionLookup:
    transaction: dict[str, Any] | None = None
    authoritative_absence: bool = False


class ExactXRPLFacilitatorScheme:
    scheme = "exact"
    caip_family = "xrpl:*"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: JsonRpcClient | Any | None = None,
        settlement_store: SettlementStore | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or JsonRpcClient(self.settings.XRPL_RPC_URL)
        self._settlement_store = settlement_store or build_settlement_store(
            self.settings, redis_client
        )
        self._supported_assets = supported_asset_keys(
            self.settings.NETWORK_ID,
            self.settings.ALLOWED_ISSUED_ASSETS,
        )

    def get_extra(self, network: str) -> dict[str, Any]:
        del network
        return {
            "areFeesSponsored": False,
            "defaultAssetTransferMethod": "sequence",
            "assetTransferMethods": ["sequence", "ticketSequence"],
            "paymentFlows": {
                "sequence": {
                    "supported": ["authorization"],
                    "default": "authorization",
                },
                "ticketSequence": {
                    "supported": ["authorization"],
                    "default": "authorization",
                },
            },
        }

    @staticmethod
    def get_signers(network: str) -> list[str]:
        del network
        return []

    def verify(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        context: Any = None,
    ) -> VerifyResponse:
        del context
        payer: str | None = None
        try:
            verified = self._verify_transaction(payload, requirements)
            return VerifyResponse(
                is_valid=True,
                payer=verified.payer,
                extra={"verificationPath": verified.verification_path},
            )
        except PaymentVerificationError as exc:
            payer = exc.payer
            reason = exc.reason
            message = exc.message
            logger.warning(
                "verification_failed", reason=reason, error=message
            )
            return VerifyResponse(
                is_valid=False,
                invalid_reason=reason,
                invalid_message=message,
                payer=payer,
            )
        except Exception:
            logger.exception("verification_infrastructure_failure")
            raise

    def settle(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        context: Any = None,
    ) -> SettleResponse:
        del context
        network = str(requirements.network)
        try:
            exact_payload = exact_xrpl_payload(payload)
            transaction_hash = signed_transaction_hash(
                exact_payload.signed_tx_blob
            )
        except Exception as exc:
            return SettleResponse(
                success=False,
                error_reason="invalid_exact_xrpl_payload",
                error_message=str(exc),
                transaction="",
                network=network,
            )

        fingerprint = requirements_fingerprint(payload, requirements)
        existing = self._settlement_store.get(transaction_hash)
        if existing is not None:
            if existing.payment_fingerprint != fingerprint:
                return SettleResponse(
                    success=False,
                    error_reason="transaction_hash_request_mismatch",
                    error_message=(
                        "The signed transaction hash is already bound to "
                        "different payment requirements"
                    ),
                    payer=existing.payer,
                    transaction=transaction_hash,
                    network=existing.network,
                )
            ttl = max(
                requirements.max_timeout_seconds + SETTLEMENT_MARGIN_SECONDS,
                300,
                self.settings.REPLAY_PROCESSED_TTL_SECONDS,
            )
            return self._reconcile(existing, requirements, ttl)

        verification = self.verify(payload, requirements)
        if not verification.is_valid:
            return SettleResponse(
                success=False,
                error_reason=(
                    verification.invalid_reason or "verification_failed"
                ),
                error_message=verification.invalid_message,
                payer=verification.payer,
                transaction="",
                network=network,
            )

        tx = binarycodec.decode(exact_payload.signed_tx_blob)
        payer = str(tx["Account"])
        last_ledger = int(tx["LastLedgerSequence"])
        first_ledger = self._current_ledger()
        ttl = max(
            requirements.max_timeout_seconds + SETTLEMENT_MARGIN_SECONDS,
            300,
            self.settings.REPLAY_PROCESSED_TTL_SECONDS,
        )
        pending = XRPLSettlementState(
            transaction=transaction_hash,
            network=network,
            payer=payer,
            first_ledger_sequence=first_ledger,
            last_ledger_sequence=last_ledger,
            payment_fingerprint=fingerprint,
            status="pending",
        )
        if not self._settlement_store.reserve(pending, ttl):
            existing = self._settlement_store.get(transaction_hash)
            if existing is None:
                return self._pending_response(
                    transaction_hash, network, payer, requirements.amount
                )
            if existing.payment_fingerprint != fingerprint:
                return SettleResponse(
                    success=False,
                    error_reason="transaction_hash_request_mismatch",
                    error_message=(
                        "The signed transaction hash is already bound to "
                        "different payment requirements"
                    ),
                    payer=existing.payer,
                    transaction=transaction_hash,
                    network=existing.network,
                )
            return self._reconcile(existing, requirements, ttl)

        if first_ledger >= last_ledger:
            return self._reconcile(pending, requirements, ttl)

        try:
            response = self.client.request(
                SubmitOnly(tx_blob=exact_payload.signed_tx_blob)
            )
            result = (
                response.result if isinstance(response.result, dict) else {}
            )
            engine_result = result.get("engine_result")
            if engine_result != "tesSUCCESS":
                logger.warning(
                    "settlement_submission_not_accepted",
                    transaction=transaction_hash,
                    engine_result=engine_result,
                )
        except Exception as exc:
            logger.warning(
                "settlement_submission_indeterminate",
                transaction=transaction_hash,
                error=str(exc),
            )
            return self._pending_response(
                transaction_hash, network, payer, requirements.amount
            )
        return self._wait_for_validation(pending, requirements, ttl)

    def _verify_transaction(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> VerifiedTransaction:
        selected_method = self._verify_envelope(payload, requirements)
        exact_payload = exact_xrpl_payload(payload)
        try:
            tx = binarycodec.decode(exact_payload.signed_tx_blob)
        except Exception as exc:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload", str(exc)
            ) from exc
        payer = str(tx.get("Account") or "")
        self._verify_signature(tx, payer)
        self._verify_structure(tx, requirements, payer)
        self._verify_ledger_and_account_state(
            tx, requirements, payer, selected_method
        )
        verification_path = self._verify_simulation_or_balances(
            tx, requirements, payer
        )
        return VerifiedTransaction(
            transaction=tx,
            transaction_hash=signed_transaction_hash(
                exact_payload.signed_tx_blob
            ),
            payer=payer,
            verification_path=verification_path,
        )

    def _verify_envelope(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> str:
        try:
            validate_requirements_shape(requirements)
        except ValueError as exc:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_requirements", str(exc)
            ) from exc
        if payload.x402_version != 2:
            raise PaymentVerificationError("invalid_x402_version")
        try:
            validate_requirements_shape(payload.accepted)
        except ValueError as exc:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_accepted_requirements", str(exc)
            ) from exc
        accepted_fields = (
            payload.accepted.scheme,
            str(payload.accepted.network),
            payload.accepted.asset,
            payload.accepted.amount,
            payload.accepted.pay_to,
            payload.accepted.max_timeout_seconds,
        )
        required_fields = (
            requirements.scheme,
            str(requirements.network),
            requirements.asset,
            requirements.amount,
            requirements.pay_to,
            requirements.max_timeout_seconds,
        )
        if accepted_fields != required_fields:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_accepted_requirements_mismatch"
            )
        accepted_extra = dict(payload.accepted.extra or {})
        required_extra = dict(requirements.extra or {})
        accepted_method = accepted_extra.pop("assetTransferMethod", None)
        required_method = required_extra.pop("assetTransferMethod", None)
        if accepted_extra != required_extra:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_accepted_requirements_mismatch"
            )
        selected_method = accepted_method or required_method or "sequence"
        if selected_method not in {"sequence", "ticketSequence"}:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_asset_transfer_method"
            )
        if (
            required_method is not None
            and selected_method != required_method
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_asset_transfer_method_mismatch"
            )
        if str(requirements.network) != self.settings.NETWORK_ID:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_network_mismatch"
            )
        if requirements.pay_to != self.settings.MY_DESTINATION_ADDRESS:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_pay_to_mismatch"
            )
        allowed = {
            (asset.code, asset.issuer) for asset in self._supported_assets
        }
        issuer = (requirements.extra or {}).get("issuer")
        code = requirements.asset
        normalized_code = (
            "XRP" if code == "XRP" else _friendly_currency(code)
        )
        if (normalized_code, issuer) not in allowed:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_asset_not_allowed"
            )
        if (
            requirements.asset == "XRP"
            and int(requirements.amount) < self.settings.MIN_XRP_DROPS
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_amount_below_minimum"
            )
        return str(selected_method)

    def _verify_signature(self, tx: dict[str, Any], payer: str) -> None:
        if tx.get("TransactionType") != "Payment":
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_transaction_type", payer=payer
            )
        if tx.get("Signers") is not None:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_multisig_not_supported",
                payer=payer,
            )
        if not is_valid_classic_address(payer):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_account", payer=payer
            )
        public_key = tx.get("SigningPubKey")
        signature = tx.get("TxnSignature")
        if (
            not isinstance(public_key, str)
            or not CANONICAL_SIGNING_PUB_KEY_PATTERN.fullmatch(public_key)
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_signing_pub_key", payer=payer
            )
        if not isinstance(signature, str) or not signature:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_signature", payer=payer
            )
        signing = dict(tx)
        signing.pop("TxnSignature", None)
        try:
            message = bytes.fromhex(binarycodec.encode_for_signing(signing))
            valid = is_valid_message(
                message, bytes.fromhex(signature), public_key
            )
        except Exception as exc:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_signature", str(exc), payer
            ) from exc
        if not valid:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_signature", payer=payer
            )

    def _verify_structure(
        self,
        tx: dict[str, Any],
        requirements: PaymentRequirements,
        payer: str,
    ) -> None:
        unexpected = set(tx) - ALLOWED_PAYMENT_FIELDS
        if unexpected:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_unexpected_fields",
                ", ".join(sorted(unexpected)),
                payer,
            )
        if tx.get("Destination") != requirements.pay_to:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_destination_mismatch", payer=payer
            )
        for field, reason in {
            "Memos": "invalid_exact_xrpl_payload_memos_not_allowed",
            "Delegate": "invalid_exact_xrpl_payload_delegate_not_allowed",
            "Paths": "invalid_exact_xrpl_payload_paths_not_allowed",
            "DeliverMin": "invalid_exact_xrpl_payload_delivermin_not_allowed",
        }.items():
            if tx.get(field) is not None:
                raise PaymentVerificationError(reason, payer=payer)
        flags = tx.get("Flags") or 0
        if isinstance(flags, int) and flags & TF_PARTIAL_PAYMENT:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_partial_payment_not_allowed",
                payer=payer,
            )
        if isinstance(flags, dict) and flags.get("tfPartialPayment"):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_partial_payment_not_allowed",
                payer=payer,
            )
        amount_fields = [
            field
            for field in ("Amount", "DeliverMax")
            if tx.get(field) is not None
        ]
        if len(amount_fields) != 1:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_ambiguous_amount_fields",
                payer=payer,
            )
        destination_amount = tx[amount_fields[0]]
        if requirements.asset == "XRP":
            if (
                not isinstance(destination_amount, str)
                or not destination_amount.isdigit()
            ):
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_amount_xrp", payer=payer
                )
            if int(destination_amount) != int(requirements.amount):
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_amount_mismatch", payer=payer
                )
            if tx.get("SendMax") is not None:
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_sendmax_not_allowed",
                    payer=payer,
                )
        else:
            self._verify_iou_amount(
                destination_amount, tx.get("SendMax"), requirements, payer
            )
        extra = requirements.extra or {}
        if tx.get("DestinationTag") != extra.get("destinationTag"):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_destination_tag_mismatch",
                payer=payer,
            )
        invoice_id = extra.get("invoiceId")
        expected_invoice = (
            invoice_id_to_invoice_id_field(invoice_id)
            if invoice_id is not None
            else None
        )
        actual_invoice = tx.get("InvoiceID")
        normalized_invoice = (
            str(actual_invoice).upper()
            if actual_invoice is not None
            else None
        )
        if normalized_invoice != expected_invoice:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_invoice_id_mismatch", payer=payer
            )
        network_id = parse_xrpl_network_id(str(requirements.network))
        if network_id <= 1024 and tx.get("NetworkID") is not None:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_network_id_for_standard_network",
                payer=payer,
            )
        if network_id > 1024 and tx.get("NetworkID") != network_id:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_network_id_mismatch", payer=payer
            )
        fee = tx.get("Fee")
        if not isinstance(fee, str) or not fee.isdigit():
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_fee_missing", payer=payer
            )
        if int(fee) > self.settings.MAX_FEE_DROPS:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_fee_too_high", payer=payer
            )

    @staticmethod
    def _verify_iou_amount(
        amount: Any,
        send_max: Any,
        requirements: PaymentRequirements,
        payer: str,
    ) -> None:
        if not isinstance(amount, dict):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_iou_amount", payer=payer
            )
        expected_currency = xrpl_currency_code(requirements.asset)
        expected_issuer = (requirements.extra or {}).get("issuer")
        if amount.get("currency") != expected_currency:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_iou_currency_mismatch",
                payer=payer,
            )
        if amount.get("issuer") != expected_issuer:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_iou_issuer_mismatch", payer=payer
            )
        if (
            compare_decimal_strings(
                str(amount.get("value")), requirements.amount
            )
            != 0
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_iou_value_mismatch", payer=payer
            )
        if not isinstance(send_max, dict):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_sendmax_required", payer=payer
            )
        if (
            send_max.get("currency") != expected_currency
            or send_max.get("issuer") != expected_issuer
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_sendmax_iou_mismatch", payer=payer
            )
        if (
            compare_decimal_strings(
                str(send_max.get("value")), requirements.amount
            )
            < 0
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_sendmax_too_low", payer=payer
            )

    def _verify_ledger_and_account_state(
        self,
        tx: dict[str, Any],
        requirements: PaymentRequirements,
        payer: str,
        selected_method: str,
    ) -> None:
        current_ledger = self._current_ledger()
        last_ledger = tx.get("LastLedgerSequence")
        if not isinstance(last_ledger, int):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_lastledgersequence_missing",
                payer=payer,
            )
        if last_ledger <= current_ledger:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_expired", payer=payer
            )
        if last_ledger > get_max_last_ledger_sequence(
            current_ledger, requirements
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_lastledgersequence_too_large",
                payer=payer,
            )
        account_data = self._account_data(payer)
        public_key_address = derive_classic_address(
            str(tx["SigningPubKey"])
        )
        flags = int(account_data.get("Flags", 0))
        regular_key = account_data.get("RegularKey")
        if public_key_address != regular_key and not (
            public_key_address == payer and not flags & LSF_DISABLE_MASTER
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_signer_not_authorized", payer=payer
            )
        if selected_method == "sequence":
            if tx.get("TicketSequence") is not None:
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_ticket_sequence_not_allowed",
                    payer=payer,
                )
            if tx.get("Sequence") != account_data.get("Sequence"):
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_sequence_not_current",
                    payer=payer,
                )
        else:
            if (
                tx.get("Sequence") != 0
                or not isinstance(tx.get("TicketSequence"), int)
            ):
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_ticket_sequence_missing",
                    payer=payer,
                )
            if not self._ticket_is_available(
                payer, int(tx["TicketSequence"])
            ):
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_ticket_not_available",
                    payer=payer,
                )

    def _ticket_is_available(self, payer: str, ticket: int) -> bool:
        marker: Any | None = None
        seen_markers: set[str] = set()
        while True:
            response = self.client.request(
                AccountObjects(
                    account=payer,
                    ledger_index="validated",
                    type=AccountObjectType.TICKET,
                    limit=400,
                    marker=marker,
                )
            )
            result = response.result
            if not isinstance(result, dict):
                raise RuntimeError(
                    "AccountObjects returned a malformed result"
                )
            objects = result.get("account_objects", [])
            if not isinstance(objects, list):
                raise RuntimeError(
                    "AccountObjects returned malformed account_objects"
                )
            if any(
                isinstance(obj, dict)
                and obj.get("TicketSequence") == ticket
                for obj in objects
            ):
                return True
            marker = result.get("marker")
            if marker is None:
                return False
            marker_key = repr(marker)
            if marker_key in seen_markers:
                raise RuntimeError(
                    "AccountObjects returned a repeated pagination marker"
                )
            seen_markers.add(marker_key)

    def _verify_simulation_or_balances(
        self,
        tx: dict[str, Any],
        requirements: PaymentRequirements,
        payer: str,
    ) -> str:
        unsigned = dict(tx)
        unsigned.pop("TxnSignature", None)
        unsigned.pop("SigningPubKey", None)
        unsigned.pop("Signers", None)
        try:
            response = self.client.request(
                Simulate(transaction=Payment.from_xrpl(unsigned))
            )
            result = (
                response.result if isinstance(response.result, dict) else {}
            )
            if _is_simulation_unavailable_result(result):
                simulation_error = (
                    result.get("error")
                    or result.get("error_message")
                    or "method unavailable"
                )
                logger.info(
                    "simulation_unavailable_using_targeted_checks",
                    error=simulation_error,
                )
                self._targeted_balance_check(tx, requirements, payer)
                return "targetedChecks"
            engine_result = result.get("engine_result")
            if engine_result != "tesSUCCESS":
                raise PaymentVerificationError(
                    (
                        "invalid_exact_xrpl_payload_simulation_failed: "
                        f"{engine_result or 'unknown'}"
                    ),
                    payer=payer,
                )
            return "simulation"
        except PaymentVerificationError:
            raise
        except Exception as exc:
            logger.info(
                "simulation_unavailable_using_targeted_checks", error=str(exc)
            )
            self._targeted_balance_check(tx, requirements, payer)
            return "targetedChecks"

    def _targeted_balance_check(
        self,
        tx: dict[str, Any],
        requirements: PaymentRequirements,
        payer: str,
    ) -> None:
        account_data = self._account_data(payer)
        balance = int(account_data["Balance"])
        reserve_base, reserve_increment = self._xrp_reserve_parameters()
        reserve = self._xrp_account_reserve(
            account_data,
            reserve_base=reserve_base,
            reserve_increment=reserve_increment,
        )
        fee = int(tx["Fee"])
        if requirements.asset == "XRP":
            required = int(requirements.amount) + fee
            if balance - reserve < required:
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_insufficient_balance",
                    payer=payer,
                )
            if (
                not self._account_exists(requirements.pay_to)
                and int(requirements.amount) < reserve_base
            ):
                raise PaymentVerificationError(
                    "invalid_exact_xrpl_payload_destination_account_creation_amount_too_low",
                    payer=payer,
                )
            return
        if balance - reserve < fee:
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_insufficient_balance",
                payer=payer,
            )
        issuer = (requirements.extra or {}).get("issuer")
        currency = xrpl_currency_code(requirements.asset)
        lines: list[dict[str, Any]] = []
        marker: Any | None = None
        seen_markers: set[str] = set()
        while True:
            result = self.client.request(
                AccountLines(
                    account=payer,
                    ledger_index="validated",
                    peer=issuer,
                    limit=400,
                    marker=marker,
                )
            ).result
            if not isinstance(result, dict) or not isinstance(
                result.get("lines", []), list
            ):
                raise RuntimeError("AccountLines returned a malformed result")
            lines.extend(
                line
                for line in result.get("lines", [])
                if isinstance(line, dict)
            )
            marker = result.get("marker")
            if marker is None:
                break
            marker_key = repr(marker)
            if marker_key in seen_markers:
                raise RuntimeError(
                    "AccountLines returned a repeated pagination marker"
                )
            seen_markers.add(marker_key)
        matching = [
            line
            for line in lines
            if line.get("account") == issuer
            and _friendly_currency(str(line.get("currency", "")))
            == _friendly_currency(currency)
        ]
        source_amount = Decimal(str(tx["SendMax"]["value"]))
        destination_amount = Decimal(requirements.amount)
        transfer_rate = self._issuer_transfer_rate(str(issuer), payer)
        if (
            payer != issuer
            and requirements.pay_to != issuer
            and source_amount * Decimal(1_000_000_000)
            < destination_amount * Decimal(transfer_rate)
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_sendmax_transfer_fee_too_low",
                payer=payer,
            )
        if (
            not matching
            or Decimal(str(matching[0].get("balance", "0")))
            < source_amount
        ):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_insufficient_trustline_balance",
                payer=payer,
            )

    def _issuer_transfer_rate(self, issuer: str, payer: str) -> int:
        response = self.client.request(
            AccountInfo(account=issuer, ledger_index="validated")
        )
        result = response.result
        account_data = (
            result.get("account_data") if isinstance(result, dict) else None
        )
        if not isinstance(account_data, dict):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_payload_iou_issuer_account_not_found",
                payer=payer,
            )
        transfer_rate = account_data.get("TransferRate", 1_000_000_000)
        if (
            not isinstance(transfer_rate, int)
            or not 1_000_000_000 <= transfer_rate <= 2_000_000_000
        ):
            raise RuntimeError("AccountInfo returned an invalid TransferRate")
        return transfer_rate

    def _account_exists(self, account: str) -> bool:
        response = self.client.request(
            AccountInfo(account=account, ledger_index="validated")
        )
        result = response.result
        if not isinstance(result, dict):
            raise RuntimeError("AccountInfo returned a malformed result")
        if isinstance(result.get("account_data"), dict):
            return True
        if result.get("error") in {"actNotFound", "accountNotFound"}:
            return False
        raise RuntimeError("AccountInfo could not determine account existence")

    def _xrp_reserve_parameters(self) -> tuple[int, int]:
        response = self.client.request(ServerState())
        result = response.result
        state = result.get("state") if isinstance(result, dict) else None
        validated = (
            state.get("validated_ledger")
            if isinstance(state, dict)
            else None
        )
        if not isinstance(validated, dict):
            raise RuntimeError("ServerState did not return a validated ledger")
        try:
            reserve_base = int(validated["reserve_base"])
            reserve_increment = int(validated["reserve_inc"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("ServerState returned invalid reserve values") from exc
        if reserve_base < 0 or reserve_increment < 0:
            raise RuntimeError("ServerState returned negative reserve values")
        return reserve_base, reserve_increment

    @staticmethod
    def _xrp_account_reserve(
        account_data: dict[str, Any],
        *,
        reserve_base: int,
        reserve_increment: int,
    ) -> int:
        owner_count = account_data.get("OwnerCount")
        if not isinstance(owner_count, int) or owner_count < 0:
            raise RuntimeError("AccountInfo returned an invalid OwnerCount")
        return reserve_base + owner_count * reserve_increment

    def _current_ledger(self) -> int:
        response = self.client.request(Ledger(ledger_index="validated"))
        return int(response.result["ledger_index"])

    def _account_data(self, account: str) -> dict[str, Any]:
        response = self.client.request(
            AccountInfo(account=account, ledger_index="validated")
        )
        data = response.result.get("account_data")
        if not isinstance(data, dict):
            raise PaymentVerificationError(
                "invalid_exact_xrpl_account_not_found", payer=account
            )
        return data

    def _wait_for_validation(
        self,
        state: XRPLSettlementState,
        requirements: PaymentRequirements,
        ttl: int,
    ) -> SettleResponse:
        for _ in range(self.settings.VALIDATION_TIMEOUT):
            lookup = self._query_transaction(state)
            response = lookup.transaction
            if response is not None:
                result_code = _transaction_result(response)
                if response.get("validated") and result_code == "tesSUCCESS":
                    delivered_error = _delivered_amount_error(
                        response, requirements
                    )
                    if delivered_error is not None:
                        failed = state.model_copy(
                            update={
                                "status": "failed",
                                "result": {
                                    "errorReason": delivered_error
                                },
                            }
                        )
                        self._settlement_store.update(failed, ttl)
                        return self._state_response(
                            failed, requirements.amount
                        )
                    completed = state.model_copy(
                        update={
                            "status": "validated",
                            "result": {"amount": requirements.amount},
                        }
                    )
                    self._settlement_store.update(completed, ttl)
                    return self._state_response(
                        completed, requirements.amount
                    )
                if response.get("validated"):
                    failed = state.model_copy(
                        update={
                            "status": "failed",
                            "result": {
                                "errorReason": (
                                    f"transaction_failed: {result_code}"
                                )
                            },
                        }
                    )
                    self._settlement_store.update(failed, ttl)
                    return self._state_response(
                        failed, requirements.amount
                    )
            time.sleep(1)
        return self._pending_response(
            state.transaction,
            state.network,
            state.payer or "",
            requirements.amount,
        )

    def _reconcile(
        self,
        state: XRPLSettlementState,
        requirements: PaymentRequirements,
        ttl: int,
    ) -> SettleResponse:
        if state.status != "pending":
            return self._state_response(state, requirements.amount)
        lookup = self._query_transaction(state)
        response = lookup.transaction
        if response is not None and response.get("validated"):
            result_code = _transaction_result(response)
            delivered_error = (
                _delivered_amount_error(response, requirements)
                if result_code == "tesSUCCESS"
                else None
            )
            status = (
                "validated"
                if result_code == "tesSUCCESS" and delivered_error is None
                else "failed"
            )
            result = (
                {"amount": requirements.amount}
                if status == "validated"
                else {
                    "errorReason": (
                        delivered_error
                        or f"transaction_failed: {result_code}"
                    )
                }
            )
            state = state.model_copy(
                update={"status": status, "result": result}
            )
            self._settlement_store.update(state, ttl)
            return self._state_response(state, requirements.amount)
        if lookup.authoritative_absence:
            try:
                current_ledger = self._current_ledger()
            except Exception as exc:
                logger.warning(
                    "settlement_ledger_lookup_indeterminate",
                    transaction=state.transaction,
                    error=str(exc),
                )
            else:
                if current_ledger >= state.last_ledger_sequence:
                    state = state.model_copy(
                        update={
                            "status": "failed",
                            "result": {
                                "errorReason": "transaction_expired"
                            },
                        }
                    )
                    self._settlement_store.update(state, ttl)
                    return self._state_response(
                        state, requirements.amount
                    )
        return self._pending_response(
            state.transaction,
            state.network,
            state.payer or "",
            requirements.amount,
        )

    def _query_transaction(
        self, state: XRPLSettlementState
    ) -> TransactionLookup:
        try:
            response = self.client.request(
                Tx(
                    transaction=state.transaction,
                    min_ledger=state.first_ledger_sequence,
                    max_ledger=state.last_ledger_sequence,
                )
            )
            result = response.result
        except Exception as exc:
            logger.warning(
                "settlement_transaction_lookup_indeterminate",
                transaction=state.transaction,
                error=str(exc),
            )
            return TransactionLookup()
        if not isinstance(result, dict):
            logger.warning(
                "settlement_transaction_lookup_malformed",
                transaction=state.transaction,
            )
            return TransactionLookup()
        error = result.get("error")
        if error is not None:
            authoritative_absence = (
                error == "txnNotFound"
                and result.get("searched_all") is True
            )
            if not authoritative_absence:
                logger.warning(
                    "settlement_transaction_lookup_uncertain",
                    transaction=state.transaction,
                    error=error,
                    searched_all=result.get("searched_all"),
                )
            return TransactionLookup(
                authoritative_absence=authoritative_absence
            )
        if not result:
            logger.warning(
                "settlement_transaction_lookup_empty",
                transaction=state.transaction,
            )
            return TransactionLookup()
        return TransactionLookup(transaction=result)

    @staticmethod
    def _pending_response(
        transaction: str, network: str, payer: str, amount: str
    ) -> SettleResponse:
        return SettleResponse(
            success=False,
            error_reason="settlement_pending",
            error_message=(
                "Transaction submission is awaiting validated ledger confirmation"
            ),
            payer=payer,
            transaction=transaction,
            network=network,
            amount=amount,
            extra={"status": "pending"},
        )

    @staticmethod
    def _state_response(
        state: XRPLSettlementState, amount: str
    ) -> SettleResponse:
        if state.status == "validated":
            return SettleResponse(
                success=True,
                payer=state.payer,
                transaction=state.transaction,
                network=state.network,
                amount=amount,
                extra={"status": "validated"},
            )
        reason = (state.result or {}).get(
            "errorReason", "transaction_failed"
        )
        return SettleResponse(
            success=False,
            error_reason=str(reason),
            payer=state.payer,
            transaction=state.transaction,
            network=state.network,
            amount=amount,
            extra={"status": state.status},
        )


class XRPLService(ExactXRPLFacilitatorScheme):
    """Backward import name for the 0.2 facilitator mechanism."""


class PaymentVerificationError(ValueError):
    def __init__(
        self,
        reason: str,
        message: str | None = None,
        payer: str | None = None,
    ) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message
        self.payer = payer


def _friendly_currency(currency: str) -> str:
    if len(currency) == 40:
        try:
            return bytes.fromhex(currency).rstrip(b"\0").decode(
                "ascii"
            ).upper()
        except (ValueError, UnicodeDecodeError):
            return currency.upper()
    return currency.upper()


def _transaction_result(result: dict[str, Any]) -> str:
    meta = result.get("meta") or result.get("metaData") or {}
    if isinstance(meta, dict):
        return str(
            meta.get("TransactionResult")
            or meta.get("transaction_result")
            or "unknown"
        )
    return "unknown"


def _delivered_amount_error(
    result: dict[str, Any], requirements: PaymentRequirements
) -> str | None:
    meta = result.get("meta") or result.get("metaData") or {}
    if not isinstance(meta, dict):
        return "transaction_failed: missing_metadata"
    delivered = meta.get(
        "delivered_amount", meta.get("DeliveredAmount")
    )
    if delivered is None or delivered == "unavailable":
        return "transaction_failed: delivered_amount_unavailable"
    if requirements.asset == "XRP":
        if (
            isinstance(delivered, str)
            and delivered.isdigit()
            and int(delivered) == int(requirements.amount)
        ):
            return None
        return "transaction_failed: delivered_amount_mismatch"
    if not isinstance(delivered, dict):
        return "transaction_failed: delivered_amount_mismatch"
    extra = requirements.extra or {}
    if (
        delivered.get("currency")
        != xrpl_currency_code(requirements.asset)
        or delivered.get("issuer") != extra.get("issuer")
        or compare_decimal_strings(
            str(delivered.get("value")), requirements.amount
        )
        != 0
    ):
        return "transaction_failed: delivered_amount_mismatch"
    return None

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any, Literal

import structlog
from xrpl.clients import JsonRpcClient
from xrpl.core import binarycodec
from xrpl.core.keypairs import derive_classic_address, is_valid_message
from xrpl.models.requests import Ledger, SubmitOnly, Tx
from xrpl.models.transactions import Payment

from xrpl_x402_core import (
    TF_PARTIAL_PAYMENT,
    AssetKey,
    NormalizedAmount,
    XRP_CODE,
    format_amount,
    normalize_currency_code,
    supported_asset_keys,
)
from xrpl_x402_facilitator.config import Settings, get_settings
from xrpl_x402_facilitator.models import AssetDescriptor, SettleResponse, StructuredAmount, VerifyResponse
from xrpl_x402_facilitator.replay_store import ReplayReservation, ReplayStore, build_replay_store

logger = structlog.get_logger()


@dataclass(frozen=True)
class ValidatedPayment:
    tx: Payment
    invoice_id: str
    blob_hash: str
    amount: NormalizedAmount
    replay_reservation: ReplayReservation | None = None


class XRPLService:
    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        replay_store: ReplayStore | None = None,
        redis_client: Any | None = None,
    ) -> None:
        self.settings = app_settings or get_settings()
        self.client = JsonRpcClient(self.settings.XRPL_RPC_URL)
        self._supported_assets = supported_asset_keys(
            self.settings.NETWORK_ID,
            self.settings.ALLOWED_ISSUED_ASSETS,
        )
        self._allowed_issued_assets = {
            asset for asset in self._supported_assets if asset.issuer is not None
        }
        self._replay_store = replay_store or build_replay_store(
            self.settings,
            redis_client=redis_client,
        )

    async def _client_request(self, request: Any) -> Any:
        # xrpl-py 4.0.0 sync client uses asyncio.run() internally; move those
        # calls off the FastAPI event loop to keep async handlers safe.
        return await asyncio.to_thread(self.client.request, request)

    @staticmethod
    def _ensure_valid_single_signer(tx_dict: dict[str, Any]) -> None:
        if tx_dict.get("Signers"):
            raise ValueError("Multisigned transactions are not supported")

        account = tx_dict.get("Account")
        if not isinstance(account, str) or not account:
            raise ValueError("Transaction Account missing")

        signing_pub_key = tx_dict.get("SigningPubKey")
        txn_signature = tx_dict.get("TxnSignature")
        if (
            not isinstance(signing_pub_key, str)
            or not signing_pub_key
            or not isinstance(txn_signature, str)
            or not txn_signature
        ):
            raise ValueError("Transaction must be signed")

        try:
            signing_address = derive_classic_address(signing_pub_key)
        except Exception as exc:
            raise ValueError("SigningPubKey invalid") from exc

        if signing_address != account:
            raise ValueError("SigningPubKey does not match Account")

        tx_for_signing = dict(tx_dict)
        tx_for_signing.pop("TxnSignature", None)

        try:
            signing_payload = bytes.fromhex(binarycodec.encode_for_signing(tx_for_signing))
            signature_bytes = bytes.fromhex(txn_signature)
            signature_valid = is_valid_message(
                signing_payload,
                signature_bytes,
                signing_pub_key,
            )
        except ValueError as exc:
            raise ValueError("Transaction signature invalid") from exc
        except Exception as exc:
            raise ValueError("Transaction signature invalid") from exc

        if not signature_valid:
            raise ValueError("Transaction signature invalid")

    @staticmethod
    def _blob_hash(signed_tx_blob: str) -> str:
        return hashlib.sha256(signed_tx_blob.encode("utf-8")).hexdigest()

    def _decode_payment(self, signed_tx_blob: str) -> Payment:
        tx_dict = binarycodec.decode(signed_tx_blob)
        if tx_dict.get("TransactionType") != "Payment":
            raise ValueError("TransactionType must be Payment")
        self._ensure_valid_single_signer(tx_dict)
        payment = Payment.from_xrpl(tx_dict)
        if not payment.is_signed():
            raise ValueError("Transaction must be signed")
        return payment

    def _resolve_invoice_id(
        self,
        payment: Payment,
        blob_hash: str,
        provided_invoice_id: str | None,
    ) -> str:
        embedded_invoice_id = payment.invoice_id
        if embedded_invoice_id:
            if provided_invoice_id and provided_invoice_id != embedded_invoice_id:
                raise ValueError("Provided invoice_id does not match transaction InvoiceID")
            return embedded_invoice_id

        if provided_invoice_id:
            raise ValueError("Provided invoice_id requires transaction InvoiceID")

        return blob_hash[:32]

    @staticmethod
    def _normalize_issued_amount_fields(
        currency: Any,
        issuer: Any,
        raw_value: Any,
    ) -> NormalizedAmount:
        normalized_currency = normalize_currency_code(str(currency))
        if normalized_currency == XRP_CODE:
            raise ValueError("XRP amounts must be expressed in drops")

        normalized_issuer = str(issuer).strip()
        if not normalized_issuer:
            raise ValueError("Issued asset issuer missing")

        if raw_value is None:
            raise ValueError("Issued asset value missing")

        try:
            value = Decimal(str(raw_value))
        except InvalidOperation as exc:
            raise ValueError("Issued asset value invalid") from exc
        if value <= 0:
            raise ValueError("Issued asset amount must be greater than zero")

        return NormalizedAmount(
            asset=AssetKey(code=normalized_currency, issuer=normalized_issuer),
            value=value,
        )

    def _normalize_amount(self, amount: Any) -> NormalizedAmount:
        if isinstance(amount, int):
            drops = amount
            if drops < 0:
                raise ValueError("Negative XRP amount not allowed")
            return NormalizedAmount(
                asset=AssetKey(code=XRP_CODE, issuer=None),
                value=Decimal(drops),
                drops=drops,
            )

        if isinstance(amount, str):
            if amount == "unavailable":
                raise ValueError("Delivered amount unavailable")
            drops = int(amount)
            if drops < 0:
                raise ValueError("Negative XRP amount not allowed")
            return NormalizedAmount(
                asset=AssetKey(code=XRP_CODE, issuer=None),
                value=Decimal(drops),
                drops=drops,
            )

        if isinstance(amount, dict):
            return self._normalize_issued_amount_fields(
                currency=amount.get("currency", ""),
                issuer=amount.get("issuer", ""),
                raw_value=amount.get("value"),
            )

        if all(hasattr(amount, field) for field in ("currency", "issuer", "value")):
            return self._normalize_issued_amount_fields(
                currency=getattr(amount, "currency"),
                issuer=getattr(amount, "issuer"),
                raw_value=getattr(amount, "value"),
            )

        raise ValueError("Unsupported payment amount format")

    def _ensure_policy(self, payment: Payment, amount: NormalizedAmount) -> None:
        raw_flags = getattr(payment, "flags", 0) or 0
        flags = int(raw_flags, 0) if isinstance(raw_flags, str) else int(raw_flags)
        if flags & TF_PARTIAL_PAYMENT:
            raise ValueError("Partial payments are not supported")

        if payment.destination != self.settings.MY_DESTINATION_ADDRESS:
            raise ValueError("Wrong destination address")

        if amount.asset.code == XRP_CODE:
            if amount.drops is None or amount.drops < self.settings.MIN_XRP_DROPS:
                raise ValueError("Payment below minimum amount")
            return

        if amount.asset not in self._allowed_issued_assets:
            raise ValueError(
                f"Unsupported issued asset: {amount.asset.code}:{amount.asset.issuer}"
            )

    @staticmethod
    def _to_asset_descriptor(asset: AssetKey) -> AssetDescriptor:
        return AssetDescriptor(code=asset.code, issuer=asset.issuer)

    @classmethod
    def _to_structured_amount(cls, amount: NormalizedAmount) -> StructuredAmount:
        return StructuredAmount(
            value=str(amount.drops if amount.drops is not None else amount.value),
            unit="drops" if amount.drops is not None else "issued",
            asset=cls._to_asset_descriptor(amount.asset),
            drops=amount.drops,
        )

    def supported_assets(self) -> list[AssetDescriptor]:
        return [self._to_asset_descriptor(asset) for asset in self._supported_assets]

    async def _get_latest_validated_ledger_sequence(self) -> int:
        response = await self._client_request(Ledger(ledger_index="validated"))
        result = getattr(response, "result", {})
        if not isinstance(result, dict):
            raise ValueError("Unable to determine current validated ledger")

        ledger_index = result.get("ledger_index")
        try:
            return int(ledger_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unable to determine current validated ledger") from exc

    async def _ensure_payment_freshness(self, payment: Payment) -> None:
        if self.settings.GATEWAY_AUTH_MODE != "redis_gateways":
            return

        last_ledger_sequence = getattr(payment, "last_ledger_sequence", None)
        if last_ledger_sequence is None:
            raise ValueError(
                "Transaction LastLedgerSequence required in redis_gateways mode"
            )

        try:
            last_ledger_sequence_int = int(last_ledger_sequence)
        except (TypeError, ValueError) as exc:
            raise ValueError("Transaction LastLedgerSequence invalid") from exc

        current_validated_ledger = await self._get_latest_validated_ledger_sequence()
        if last_ledger_sequence_int <= current_validated_ledger:
            raise ValueError("Transaction LastLedgerSequence expired")

        max_allowed_ledger = (
            current_validated_ledger + self.settings.MAX_PAYMENT_LEDGER_WINDOW
        )
        if last_ledger_sequence_int > max_allowed_ledger:
            raise ValueError("Transaction LastLedgerSequence too far in the future")

    async def _validate_payment(
        self,
        signed_tx_blob: str,
        provided_invoice_id: str | None,
        replay_mode: Literal["verify", "settle"],
    ) -> ValidatedPayment:
        tx = self._decode_payment(signed_tx_blob)
        blob_hash = self._blob_hash(signed_tx_blob)
        invoice_id = self._resolve_invoice_id(tx, blob_hash, provided_invoice_id)
        amount = self._normalize_amount(tx.amount)
        self._ensure_policy(tx, amount)
        await self._ensure_payment_freshness(tx)
        replay_reservation: ReplayReservation | None = None
        if replay_mode == "settle":
            replay_reservation = await self._replay_store.reserve(invoice_id, blob_hash)
        else:
            await self._replay_store.guard_available(invoice_id, blob_hash)
        return ValidatedPayment(
            tx=tx,
            invoice_id=invoice_id,
            blob_hash=blob_hash,
            amount=amount,
            replay_reservation=replay_reservation,
        )

    def _extract_delivered_amount(self, result: dict[str, Any]) -> NormalizedAmount:
        meta = result.get("meta") or {}
        delivered_amount = meta.get("delivered_amount")
        if delivered_amount is None:
            delivered_amount = meta.get("DeliveredAmount")
        if delivered_amount is None:
            raise ValueError("Validated transaction missing delivered_amount")
        return self._normalize_amount(delivered_amount)

    @staticmethod
    def _ensure_delivered_amount_matches(
        expected: NormalizedAmount,
        delivered: NormalizedAmount,
    ) -> None:
        if expected.asset != delivered.asset:
            raise ValueError("Validated transaction delivered unexpected asset")

        if expected.drops is not None:
            if delivered.drops is None or expected.drops != delivered.drops:
                raise ValueError("Validated transaction delivered wrong XRP amount")
            return

        if delivered.drops is not None or expected.value != delivered.value:
            raise ValueError("Validated transaction delivered wrong issued-asset amount")

    @staticmethod
    def _submit_failure_detail(result: dict[str, Any], *fallbacks: Any) -> str:
        for candidate in (
            result.get("engine_result_message"),
            result.get("error_message"),
            result.get("error"),
            *fallbacks,
        ):
            if candidate is None:
                continue
            rendered = getattr(candidate, "value", candidate)
            if isinstance(rendered, str):
                rendered = rendered.strip()
                if rendered:
                    return rendered
                continue
            return str(rendered)
        return "unknown submission failure"

    @classmethod
    def _ensure_submit_succeeded(cls, response: Any) -> dict[str, Any]:
        result = getattr(response, "result", {})
        if not isinstance(result, dict):
            result = {}

        status = getattr(response, "status", None)
        status_value = getattr(status, "value", status)
        if status_value is not None and status_value != "success":
            detail = cls._submit_failure_detail(result, status_value)
            raise ValueError(f"XRPL submission failed: {detail}")

        engine_result = result.get("engine_result")
        if not isinstance(engine_result, str):
            detail = cls._submit_failure_detail(result)
            if detail == "unknown submission failure":
                raise ValueError("XRPL submission failed: missing engine_result")
            raise ValueError(
                f"XRPL submission failed: missing engine_result ({detail})"
            )

        if engine_result != "tesSUCCESS":
            detail = cls._submit_failure_detail(result)
            if detail != engine_result:
                raise ValueError(f"XRPL submission rejected: {engine_result} ({detail})")
            raise ValueError(f"XRPL submission rejected: {engine_result}")

        return result

    async def verify_payment(
        self,
        signed_tx_blob: str,
        provided_invoice_id: str | None = None,
    ) -> VerifyResponse:
        try:
            validated_payment = await self._validate_payment(
                signed_tx_blob,
                provided_invoice_id,
                replay_mode="verify",
            )
            return VerifyResponse(
                valid=True,
                invoice_id=validated_payment.invoice_id,
                amount=format_amount(validated_payment.amount),
                asset=self._to_asset_descriptor(validated_payment.amount.asset),
                amount_details=self._to_structured_amount(validated_payment.amount),
                payer=validated_payment.tx.account,
                destination=validated_payment.tx.destination,
            )
        except Exception as exc:
            logger.warning("verification_failed", error=str(exc))
            raise ValueError(f"Invalid payment: {exc}") from exc

    async def settle_payment(
        self,
        signed_tx_blob: str,
        provided_invoice_id: str | None = None,
    ) -> SettleResponse:
        validated_payment: ValidatedPayment | None = None
        try:
            validated_payment = await self._validate_payment(
                signed_tx_blob,
                provided_invoice_id,
                replay_mode="settle",
            )

            response = await self._client_request(SubmitOnly(tx_blob=signed_tx_blob))
            self._ensure_submit_succeeded(response)
            tx_hash = validated_payment.tx.get_hash()

            if self.settings.SETTLEMENT_MODE == "validated":
                for _ in range(self.settings.VALIDATION_TIMEOUT):
                    tx_info = await self._client_request(Tx(transaction=tx_hash))
                    if tx_info.result.get("validated"):
                        reservation = validated_payment.replay_reservation
                        if reservation is None:
                            raise ValueError("Replay reservation missing for settlement")
                        await self._replay_store.mark_processed(
                            reservation
                        )
                        delivered_amount = self._extract_delivered_amount(tx_info.result)
                        self._ensure_delivered_amount_matches(
                            validated_payment.amount,
                            delivered_amount,
                        )
                        logger.info("payment_validated", tx_hash=tx_hash)
                        return SettleResponse(settled=True, tx_hash=tx_hash, status="validated")
                    await asyncio.sleep(1)

                reservation = validated_payment.replay_reservation
                if reservation is not None:
                    await self._replay_store.release_pending(reservation)
                raise ValueError("Validation timeout exceeded")

            reservation = validated_payment.replay_reservation
            if reservation is None:
                raise ValueError("Replay reservation missing for settlement")
            await self._replay_store.mark_processed(reservation)
            logger.info("payment_submitted", tx_hash=tx_hash)
            return SettleResponse(settled=True, tx_hash=tx_hash, status="submitted")
        except Exception as exc:
            if validated_payment is not None and validated_payment.replay_reservation is not None:
                await self._replay_store.release_pending(validated_payment.replay_reservation)
            logger.error("settlement_failed", error=str(exc))
            raise ValueError(str(exc)) from exc

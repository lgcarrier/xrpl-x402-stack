from __future__ import annotations

import json
from typing import Any

from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import Payment
from xrpl.transaction import autofill, sign
from xrpl.wallet import Wallet

from xrpl_x402_core import (
    PaymentPayload,
    PaymentRequired,
    XRPLAmount,
    XRPLAsset,
    XRPLPaymentOption,
    XRPLPaymentPayload,
    canonical_asset_identifier,
    decode_model_from_base64,
    encode_model_to_base64,
    parse_asset_identifier,
    xrpl_currency_code,
)

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


def decode_payment_required(raw_header: str) -> PaymentRequired:
    return decode_model_from_base64(raw_header, PaymentRequired)


def select_payment_option(
    payment_required: PaymentRequired,
    *,
    network: str | None = None,
    asset: str | XRPLAsset | None = None,
) -> XRPLPaymentOption:
    candidates = [option for option in payment_required.accepts if option.scheme == "exact"]

    if network is not None:
        candidates = [option for option in candidates if option.network == network]

    if asset is not None:
        asset_identifier = (
            canonical_asset_identifier(asset)
            if isinstance(asset, XRPLAsset)
            else canonical_asset_identifier(_asset_from_identifier(asset))
        )
        candidates = [
            option
            for option in candidates
            if canonical_asset_identifier(option.asset) == asset_identifier
        ]

    if not candidates:
        raise ValueError("No matching XRPL payment option found")
    return candidates[0]


class XRPLPaymentSigner:
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
    ) -> None:
        self.wallet = wallet
        self.network = network
        self._client = client or JsonRpcClient(rpc_url)
        self._autofill_enabled = autofill_enabled
        self._default_fee = default_fee
        self._default_sequence = default_sequence
        self._default_last_ledger_sequence = default_last_ledger_sequence

    def build_payment_payload(
        self,
        option: XRPLPaymentOption,
        *,
        invoice_id: str | None = None,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
    ) -> PaymentPayload:
        if self.network is not None and option.network != self.network:
            raise ValueError(
                f"Payment option network {option.network} does not match signer network {self.network}"
            )

        signed_tx_blob = self.sign_option(
            option,
            invoice_id=invoice_id,
            fee=fee,
            sequence=sequence,
            last_ledger_sequence=last_ledger_sequence,
        )
        return PaymentPayload(
            network=option.network,
            payload=XRPLPaymentPayload(
                signedTxBlob=signed_tx_blob,
                invoiceId=invoice_id,
            ),
        )

    def build_x402_payload(
        self,
        *,
        network: str,
        asset_identifier: str,
        amount: str,
        pay_to: str,
        invoice_id: str | None = None,
        description: str | None = None,
        mime_type: str = "application/json",
        expires_at: int | None = None,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
    ) -> PaymentPayload:
        asset_key = parse_asset_identifier(asset_identifier)
        asset = XRPLAsset(code=asset_key.code, issuer=asset_key.issuer)
        xrpl_amount = _amount_from_identifier(asset, amount)
        option = XRPLPaymentOption(
            network=network,
            payTo=pay_to,
            maxAmountRequired=xrpl_amount.value,
            asset=asset,
            amount=xrpl_amount,
            description=description,
            mimeType=mime_type,
            expiresAt=expires_at,
        )
        return self.build_payment_payload(
            option,
            invoice_id=invoice_id,
            fee=fee,
            sequence=sequence,
            last_ledger_sequence=last_ledger_sequence,
        )

    def sign_option(
        self,
        option: XRPLPaymentOption,
        *,
        invoice_id: str | None = None,
        fee: str | None = None,
        sequence: int | None = None,
        last_ledger_sequence: int | None = None,
    ) -> str:
        payment_kwargs: dict[str, Any] = {
            "account": self.wallet.classic_address,
            "destination": option.pay_to,
            "amount": _to_xrpl_amount(option.asset, option.amount),
        }
        if invoice_id is not None:
            payment_kwargs["invoice_id"] = invoice_id

        if self._autofill_enabled:
            payment = Payment(**payment_kwargs)
            signed_payment = sign(autofill(payment, self._client), self.wallet)
            return signed_payment.blob()

        payment_kwargs["fee"] = fee or self._default_fee
        payment_kwargs["sequence"] = (
            sequence if sequence is not None else self._default_sequence
        )
        if last_ledger_sequence is not None:
            payment_kwargs["last_ledger_sequence"] = last_ledger_sequence
        elif self._default_last_ledger_sequence is not None:
            payment_kwargs["last_ledger_sequence"] = self._default_last_ledger_sequence
        payment = Payment(**payment_kwargs)
        signed_payment = sign(payment, self.wallet)
        return signed_payment.blob()


def build_payment_signature(
    payment_required: PaymentRequired | XRPLPaymentOption,
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    asset: str | XRPLAsset | None = None,
    invoice_id: str | None = None,
) -> str:
    selected_network = network or signer.network
    option = (
        select_payment_option(payment_required, network=selected_network, asset=asset)
        if isinstance(payment_required, PaymentRequired)
        else payment_required
    )
    payload = signer.build_payment_payload(option, invoice_id=invoice_id)
    return encode_model_to_base64(payload)


def decode_payment_required_response(
    *,
    headers: dict[str, str],
    body: bytes | None,
) -> PaymentRequired:
    header_value = headers.get(PAYMENT_REQUIRED_HEADER)
    if header_value is None:
        header_value = headers.get(PAYMENT_REQUIRED_HEADER.lower())
    if header_value:
        return decode_payment_required(header_value)
    if body is None:
        raise ValueError("402 response did not include a PAYMENT-REQUIRED header or JSON body")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("402 response body is not valid JSON") from exc
    return PaymentRequired.model_validate(data)


def _asset_from_identifier(identifier: str) -> XRPLAsset:
    asset = parse_asset_identifier(identifier)
    return XRPLAsset(code=asset.code, issuer=asset.issuer)


def _amount_from_identifier(asset: XRPLAsset, amount: str) -> XRPLAmount:
    if canonical_asset_identifier(asset) == "XRP:native":
        drops = int(amount)
        return XRPLAmount(value=str(drops), unit="drops", drops=drops)
    return XRPLAmount(value=amount, unit="issued")


def _to_xrpl_amount(asset: XRPLAsset, amount: XRPLAmount) -> str | IssuedCurrencyAmount:
    if amount.unit == "drops":
        return amount.value
    if asset.issuer is None:
        raise ValueError("Issued-asset payments require an issuer")
    return IssuedCurrencyAmount(
        currency=xrpl_currency_code(asset.code),
        issuer=asset.issuer,
        value=amount.value,
    )

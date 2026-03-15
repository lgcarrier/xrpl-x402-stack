from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
from typing import Any

import httpx
from xrpl.wallet import Wallet

from xrpl_x402_client import (
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    XRPLPaymentSigner,
    build_payment_signature,
    decode_payment_required_response,
    select_payment_option,
)
from xrpl_x402_core import (
    NETWORK_RLUSD_ISSUERS,
    NETWORK_USDC_ISSUERS,
    PaymentRequired,
    PaymentResponse,
    asset_identifier_from_parts,
    canonical_asset_identifier,
    decode_model_from_base64,
    normalize_currency_code,
)

from xrpl_x402_payer.receipts import ReceiptRecord, ReceiptStore

DEFAULT_RPC_URL = "https://s.altnet.rippletest.net:51234"
DEFAULT_NETWORK = "xrpl:1"
DEFAULT_MAX_SPEND_ENV = "XRPL_X402_MAX_SPEND"
DEFAULT_TIMEOUT = 20.0


@dataclass(slots=True)
class PayResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    challenge_present: bool
    dry_run: bool
    paid: bool
    preview: dict[str, Any] | None = None
    receipt: ReceiptRecord | None = None
    payment_response: PaymentResponse | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class XRPLPayer:
    def __init__(
        self,
        signer: XRPLPaymentSigner | None,
        *,
        network: str | None = None,
        store: ReceiptStore | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.signer = signer
        self.network = network or (signer.network if signer is not None else None) or DEFAULT_NETWORK
        self.store = store or ReceiptStore()
        self.timeout = timeout

    async def pay(
        self,
        *,
        url: str,
        amount: float | Decimal = Decimal("0.001"),
        asset: str = "XRP",
        issuer: str | None = None,
        max_spend: float | Decimal | None = None,
        dry_run: bool = False,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> PayResult:
        asset_identifier = resolve_asset_identifier(asset=asset, issuer=issuer, network=self.network)
        spend_cap = resolve_spend_cap(amount=amount, max_spend=max_spend)
        request_headers = dict(headers or {})

        async with httpx.AsyncClient(transport=transport, timeout=self.timeout) as client:
            initial_response = await client.request(
                method=method,
                url=url,
                headers=request_headers,
                content=content,
            )
            await initial_response.aread()

            payment_required = _decode_payment_required(initial_response)

            if dry_run:
                preview = build_dry_run_preview(
                    response=initial_response,
                    payment_required=payment_required,
                    network=self.network,
                    asset_identifier=asset_identifier,
                    spend_cap=spend_cap,
                )
                return PayResult(
                    status_code=initial_response.status_code,
                    body=initial_response.content,
                    headers=dict(initial_response.headers),
                    challenge_present=payment_required is not None,
                    dry_run=True,
                    paid=False,
                    preview=preview,
                )

            if initial_response.status_code == 402 and payment_required is None:
                raise ValueError("402 response did not include a valid x402 challenge")

            if payment_required is None:
                return PayResult(
                    status_code=initial_response.status_code,
                    body=initial_response.content,
                    headers=dict(initial_response.headers),
                    challenge_present=False,
                    dry_run=False,
                    paid=False,
                )

            option = select_payment_option(
                payment_required,
                network=self.network,
                asset=asset_identifier,
            )
            option_amount = payment_option_amount(option)
            if spend_cap is not None and option_amount > spend_cap:
                raise ValueError(
                    f"Payment amount {option_amount} exceeds configured spend cap {spend_cap}"
                )

            if self.signer is None:
                raise RuntimeError("XRPL_WALLET_SEED is required to pay x402 resources")
            payment_signature = build_payment_signature(
                payment_required,
                self.signer,
                network=self.network,
                asset=asset_identifier,
            )
            retry_headers = dict(request_headers)
            retry_headers[PAYMENT_SIGNATURE_HEADER] = payment_signature
            retry_response = await client.request(
                method=method,
                url=url,
                headers=retry_headers,
                content=content,
            )
            await retry_response.aread()
            payment_response = decode_payment_response(retry_response.headers)
            receipt = None
            if payment_response is not None:
                receipt = build_receipt_record(
                    url=url,
                    method=method,
                    status_code=retry_response.status_code,
                    payment_response=payment_response,
                )
                self.store.append(receipt)

            return PayResult(
                status_code=retry_response.status_code,
                body=retry_response.content,
                headers=dict(retry_response.headers),
                challenge_present=True,
                dry_run=False,
                paid=payment_response is not None,
                receipt=receipt,
                payment_response=payment_response,
            )


async def pay_with_x402(**kwargs: Any) -> PayResult:
    signer = kwargs.pop("signer", None)
    rpc_url = kwargs.pop("rpc_url", None)
    dry_run = bool(kwargs.get("dry_run", False))
    if signer is None and not dry_run:
        signer = build_signer_from_env(
            rpc_url=rpc_url,
            network=kwargs.get("network"),
        )
    network = kwargs.pop("network", None) or (signer.network if signer is not None else None) or DEFAULT_NETWORK
    store = kwargs.pop("store", None)
    payer = XRPLPayer(signer, network=network, store=store)
    return await payer.pay(**kwargs)


def build_signer_from_env(
    *,
    rpc_url: str | None = None,
    network: str | None = None,
) -> XRPLPaymentSigner:
    wallet_seed = os.getenv("XRPL_WALLET_SEED", "").strip()
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to pay x402 resources")

    wallet = Wallet.from_seed(wallet_seed)
    resolved_network = network or os.getenv("XRPL_NETWORK") or os.getenv("NETWORK_ID") or DEFAULT_NETWORK
    resolved_rpc_url = rpc_url or os.getenv("XRPL_RPC_URL") or DEFAULT_RPC_URL
    return XRPLPaymentSigner(wallet, rpc_url=resolved_rpc_url, network=resolved_network)


def resolve_asset_identifier(*, asset: str, issuer: str | None, network: str) -> str:
    normalized_asset = normalize_currency_code(asset)
    if normalized_asset == "XRP":
        return "XRP:native"

    normalized_issuer = issuer.strip() if issuer else None
    if normalized_issuer is None and normalized_asset == "RLUSD":
        normalized_issuer = NETWORK_RLUSD_ISSUERS.get(network)
    if normalized_issuer is None and normalized_asset == "USDC":
        normalized_issuer = NETWORK_USDC_ISSUERS.get(network)
    if normalized_issuer is None:
        raise ValueError(f"Issuer is required for asset {normalized_asset}")
    return asset_identifier_from_parts(normalized_asset, normalized_issuer)


def resolve_spend_cap(
    *,
    amount: float | Decimal,
    max_spend: float | Decimal | None,
) -> Decimal | None:
    if max_spend is not None:
        return Decimal(str(max_spend))

    env_cap = os.getenv(DEFAULT_MAX_SPEND_ENV, "").strip()
    if env_cap:
        return Decimal(env_cap)
    return Decimal(str(amount))


def payment_option_amount(option: Any) -> Decimal:
    if option.amount.unit == "drops":
        return Decimal(option.amount.drops) / Decimal("1000000")
    return Decimal(option.amount.value)


def decode_payment_response(headers: httpx.Headers | dict[str, str]) -> PaymentResponse | None:
    response_header = headers.get(PAYMENT_RESPONSE_HEADER)
    if response_header is None:
        response_header = headers.get(PAYMENT_RESPONSE_HEADER.lower())
    if not response_header:
        return None
    return decode_model_from_base64(response_header, PaymentResponse)


def build_receipt_record(
    *,
    url: str,
    method: str,
    status_code: int,
    payment_response: PaymentResponse,
) -> ReceiptRecord:
    return ReceiptRecord(
        created_at=datetime.now(UTC).isoformat(),
        url=url,
        method=method.upper(),
        status_code=status_code,
        network=payment_response.network,
        asset_identifier=canonical_asset_identifier(payment_response.asset),
        amount=payment_response_amount(payment_response),
        payer=payment_response.payer,
        tx_hash=payment_response.tx_hash,
        settlement_status=payment_response.settlement_status,
    )


def payment_response_amount(payment_response: PaymentResponse) -> str:
    if payment_response.amount.unit == "drops":
        return str(Decimal(payment_response.amount.drops) / Decimal("1000000"))
    return payment_response.amount.value


def build_dry_run_preview(
    *,
    response: httpx.Response,
    payment_required: PaymentRequired | None,
    network: str,
    asset_identifier: str,
    spend_cap: Decimal | None,
) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "mode": "dry_run",
        "status_code": response.status_code,
        "url": str(response.request.url),
        "network": network,
        "asset_identifier": asset_identifier,
        "spend_cap": str(spend_cap) if spend_cap is not None else None,
        "x402_challenge_present": payment_required is not None,
    }
    if payment_required is None:
        preview["message"] = "No valid x402 challenge detected; no payment attempted."
        return preview

    option = select_payment_option(payment_required, network=network, asset=asset_identifier)
    option_amount = payment_option_amount(option)
    preview["selected_payment"] = {
        "pay_to": option.pay_to,
        "amount": str(option_amount),
        "asset_identifier": canonical_asset_identifier(option.asset),
    }
    preview["would_pay"] = spend_cap is None or option_amount <= spend_cap
    return preview


def format_pay_result(result: PayResult) -> str:
    if result.preview is not None:
        return json.dumps(result.preview, indent=2, sort_keys=True)
    if result.text.strip():
        return result.text

    summary = {
        "status_code": result.status_code,
        "paid": result.paid,
        "receipt": result.receipt.model_dump() if result.receipt is not None else None,
    }
    return json.dumps(summary, indent=2, sort_keys=True)


def get_receipts(limit: int = 10, *, store: ReceiptStore | None = None) -> list[dict[str, Any]]:
    active_store = store or ReceiptStore()
    return [receipt.model_dump() for receipt in active_store.list(limit=limit)]


def budget_status(
    *,
    asset: str = "XRP",
    issuer: str | None = None,
    network: str | None = None,
    store: ReceiptStore | None = None,
) -> dict[str, str | None]:
    resolved_network = network or os.getenv("XRPL_NETWORK") or os.getenv("NETWORK_ID") or DEFAULT_NETWORK
    asset_identifier = resolve_asset_identifier(asset=asset, issuer=issuer, network=resolved_network)
    env_cap = os.getenv(DEFAULT_MAX_SPEND_ENV, "").strip()
    max_spend = Decimal(env_cap) if env_cap else None
    active_store = store or ReceiptStore()
    return active_store.budget_summary(asset_identifier=asset_identifier, max_spend=max_spend)


def _decode_payment_required(response: httpx.Response) -> PaymentRequired | None:
    if response.status_code != 402:
        return None
    try:
        return decode_payment_required_response(
            headers=dict(response.headers),
            body=response.content,
        )
    except ValueError:
        return None

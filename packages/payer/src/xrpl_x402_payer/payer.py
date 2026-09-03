from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import os
from typing import Any

import httpx
from x402 import x402Client
from x402.extensions.payment_identifier import extract_payment_identifier
from x402.http import (
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_payment_response_header,
    safe_base64_encode,
)
from x402.schemas import (
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResponse,
)
from xrpl.wallet import Wallet

from xrpl_x402_client import (
    XRPLAssetSpendLimit,
    XRPLPaymentSigner,
    apply_xrpl_spend_limits,
    decode_payment_required_response,
    register_exact_xrpl_client,
    select_payment_option,
)
from xrpl_x402_core import (
    exact_xrpl_payload,
    find_default_asset,
    normalize_currency_code,
    signed_transaction_hash,
)
from xrpl_x402_core.testnet_rpc import resolve_testnet_rpc_url
from xrpl_x402_payer.journal import AttemptClaim
from xrpl_x402_payer.receipts import ReceiptRecord, ReceiptStore

MAINNET_RPC_URL = "https://s1.ripple.com:51234"
DEVNET_RPC_URL = "https://s.devnet.rippletest.net:51234"
DEFAULT_NETWORK = "xrpl:1"
DEFAULT_TIMEOUT = 20.0


@dataclass(slots=True)
class PayResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    challenge_present: bool
    dry_run: bool
    paid: bool
    pending: bool = False
    preview: dict[str, Any] | None = None
    receipt: ReceiptRecord | None = None
    payment_response: SettleResponse | None = None
    accepted: PaymentRequirements | None = None

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
        payer_account: str | None = None,
    ) -> None:
        signer_account = signer.classic_address if signer is not None else None
        if (
            signer_account is not None
            and payer_account is not None
            and signer_account != payer_account
        ):
            raise ValueError("payer_account does not match the signer")
        self.signer = signer
        self.payer_account = payer_account or signer_account
        self.network = network or (
            signer.network if signer is not None else None
        ) or DEFAULT_NETWORK
        self.store = store or ReceiptStore()
        self.timeout = timeout

    async def pay(
        self,
        *,
        url: str,
        asset: str | None = None,
        issuer: str | None = None,
        max_spend: str | int | Decimal | None = None,
        dry_run: bool = False,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> PayResult:
        request_headers = dict(headers or {})
        limit = _explicit_limit(
            network=self.network,
            asset=asset,
            issuer=issuer,
            max_spend=max_spend,
        )
        if dry_run:
            async with httpx.AsyncClient(
                transport=transport, timeout=self.timeout
            ) as plain:
                response = await plain.request(
                    method, url, headers=request_headers, content=content
                )
                await response.aread()
            challenge = _decode_payment_required(response)
            preview = _preview(challenge, self.network, limit)
            return PayResult(
                status_code=response.status_code,
                body=response.content,
                headers=dict(response.headers),
                challenge_present=challenge is not None,
                dry_run=True,
                paid=False,
                preview=preview,
            )

        payer_account = self.payer_account
        if payer_account is None:
            raise RuntimeError(
                "A signer or explicit payer_account is required for durable payment recovery"
            )
        request_fingerprint = _request_fingerprint(
            payer=payer_account,
            url=url,
            method=method,
            headers=request_headers,
            content=content,
        )
        async with httpx.AsyncClient(
            transport=transport, timeout=self.timeout
        ) as client:
            response = await client.request(
                method, url, headers=request_headers, content=content
            )
            await response.aread()
            if response.status_code != 402:
                return PayResult(
                    status_code=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                    challenge_present=False,
                    dry_run=False,
                    paid=False,
                )

            try:
                payment_required = decode_payment_required_response(
                    headers=dict(response.headers)
                )
            except ValueError as exc:
                raise ValueError(
                    "Only canonical x402 v2 PAYMENT-REQUIRED headers are supported"
                ) from exc
            claim = await _claim_or_wait(
                self.store,
                fingerprint=request_fingerprint,
                payer=payer_account,
                recovery_scope="http",
            )
            payload = claim.attempt.payment_payload
            try:
                if payload is not None:
                    _validate_payload_for_challenge(payload, payment_required)
                else:
                    if self.signer is None:
                        raise RuntimeError(
                            "XRPL_WALLET_SEED is required to create a new x402 payment"
                        )
                    payment_client = x402Client()
                    register_exact_xrpl_client(
                        payment_client, self.signer, self.network
                    )
                    if limit is not None:
                        apply_xrpl_spend_limits(payment_client, [limit])
                    else:
                        # Upstream defaults: recognized pegged assets only,
                        # with a USD 1 per-payment cap.
                        payment_client.set_spend_controls(
                            {"max_amount_per_payment": "$1"}
                        )
                    created = await payment_client.create_payment_payload(
                        payment_required
                    )
                    if not isinstance(created, PaymentPayload):
                        raise TypeError("XRPL payer only supports x402 v2 payloads")
                    attempt = self.store.persist_attempt_payload(
                        fingerprint=request_fingerprint,
                        owner_token=claim.owner_token,
                        payment_payload=created,
                        payment_identifier=extract_payment_identifier(created),
                    )
                    payload = attempt.payment_payload
                    if payload is None:  # pragma: no cover - model invariant
                        raise RuntimeError("Persisted payment payload disappeared")

                # The exact signed payload and payment identifier are durable
                # before this paid request crosses the transport boundary.
                paid_headers = dict(request_headers)
                paid_headers[PAYMENT_SIGNATURE_HEADER] = (
                    _encode_payment_payload_header(payload)
                )
                response = await client.request(
                    method,
                    url,
                    headers=paid_headers,
                    content=content,
                )
                await response.aread()
            except BaseException:
                # Once persisted, a payload may have crossed the network. Keep
                # it recoverable and record a standard local pending outcome.
                if payload is not None:
                    self.store.record_outcome(
                        ReceiptRecord(
                            created_at=datetime.now(UTC).isoformat(),
                            url=url,
                            method=method.upper(),
                            status_code=0,
                            state="pending",
                            settlement=_pending_settlement(payload),
                            accepted=payload.accepted,
                            request_fingerprint=request_fingerprint,
                        )
                    )
                self.store.release_claim(
                    request_fingerprint, claim.owner_token
                )
                raise

        settlement = _decode_settlement(response)
        if settlement is None:
            settlement = _pending_settlement(payload)
            receipt = ReceiptRecord(
                created_at=datetime.now(UTC).isoformat(),
                url=url,
                method=method.upper(),
                status_code=response.status_code,
                state="pending",
                settlement=settlement,
                accepted=payload.accepted,
                request_fingerprint=request_fingerprint,
            )
            self.store.record_outcome(receipt)
            return PayResult(
                status_code=response.status_code,
                body=b"",
                headers=dict(response.headers),
                challenge_present=True,
                dry_run=False,
                paid=False,
                pending=True,
                receipt=receipt,
                payment_response=settlement,
                accepted=payload.accepted,
            )
        pending = bool(
            not settlement.success
            and settlement.error_reason == "settlement_pending"
        )
        state = (
            "paid"
            if settlement.success
            else "pending"
            if pending
            else "failed"
        )
        receipt = ReceiptRecord(
            created_at=datetime.now(UTC).isoformat(),
            url=url,
            method=method.upper(),
            status_code=response.status_code,
            state=state,
            settlement=settlement,
            accepted=payload.accepted,
            request_fingerprint=request_fingerprint,
        )
        self.store.record_outcome(receipt)
        return PayResult(
            status_code=response.status_code,
            body=response.content if settlement.success else b"",
            headers=dict(response.headers),
            challenge_present=True,
            dry_run=False,
            paid=settlement.success,
            pending=pending,
            receipt=receipt,
            payment_response=settlement,
            accepted=payload.accepted,
        )


async def pay_with_x402(**kwargs: Any) -> PayResult:
    signer = kwargs.pop("signer", None)
    payer_account = kwargs.pop("payer_account", None)
    rpc_url = kwargs.pop("rpc_url", None)
    dry_run = bool(kwargs.get("dry_run", False))
    network = kwargs.pop("network", None) or DEFAULT_NETWORK
    if signer is None and not dry_run and payer_account is None:
        signer = build_signer_from_env(rpc_url=rpc_url, network=network)
    payer = XRPLPayer(
        signer,
        network=network,
        store=kwargs.pop("store", None),
        payer_account=payer_account,
    )
    return await payer.pay(**kwargs)


def build_signer_from_env(
    *, rpc_url: str | None = None, network: str | None = None
) -> XRPLPaymentSigner:
    wallet_seed = os.getenv("XRPL_WALLET_SEED", "").strip()
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to pay x402 resources")
    resolved_network = (
        network
        or os.getenv("XRPL_NETWORK")
        or os.getenv("NETWORK_ID")
        or DEFAULT_NETWORK
    )
    resolved_rpc = (rpc_url or os.getenv("XRPL_RPC_URL", "")).strip()
    if not resolved_rpc:
        if resolved_network == "xrpl:0":
            resolved_rpc = MAINNET_RPC_URL
        elif resolved_network == "xrpl:1":
            resolved_rpc = resolve_testnet_rpc_url()
        elif resolved_network == "xrpl:2":
            resolved_rpc = DEVNET_RPC_URL
        else:
            raise RuntimeError(
                "XRPL_RPC_URL is required for custom XRPL networks"
            )
    return XRPLPaymentSigner(
        Wallet.from_seed(wallet_seed),
        rpc_url=resolved_rpc,
        network=resolved_network,
    )


def _explicit_limit(
    *,
    network: str,
    asset: str | None,
    issuer: str | None,
    max_spend: str | int | Decimal | None,
) -> XRPLAssetSpendLimit | None:
    if asset is None:
        if issuer is not None or max_spend is not None:
            raise ValueError(
                "asset is required when issuer or max_spend is configured"
            )
        return None
    code = normalize_currency_code(asset)
    if max_spend is None:
        raise ValueError(
            "XRP, USDC, and custom IOUs require an explicit per-payment cap"
        )
    if code != "XRP" and not issuer:
        raise ValueError("explicit IOU payments require an issuer")
    if code == "XRP" and issuer is not None:
        raise ValueError("XRP does not have an issuer")
    return XRPLAssetSpendLimit(
        network=network,
        asset=code,
        issuer=issuer,
        max_amount=str(max_spend),
    )


def _decode_payment_required(response: httpx.Response) -> PaymentRequired | None:
    if response.status_code != 402:
        return None
    try:
        return decode_payment_required_response(headers=dict(response.headers))
    except ValueError:
        return None


def _decode_settlement(response: httpx.Response) -> SettleResponse | None:
    raw = response.headers.get(PAYMENT_RESPONSE_HEADER)
    if not raw:
        return None
    parsed = decode_payment_response_header(raw)
    return parsed if isinstance(parsed, SettleResponse) else None


def _pending_settlement(payload: PaymentPayload) -> SettleResponse:
    signed_blob = exact_xrpl_payload(payload).signed_tx_blob
    return SettleResponse(
        success=False,
        error_reason="settlement_pending",
        transaction=signed_transaction_hash(signed_blob),
        network=str(payload.accepted.network),
        amount=payload.accepted.amount,
    )


def _encode_payment_payload_header(payload: PaymentPayload) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return safe_base64_encode(canonical)


async def _claim_or_wait(
    store: ReceiptStore,
    *,
    fingerprint: str,
    payer: str,
    recovery_scope: str,
) -> AttemptClaim:
    """Wait for the active signer or take over an expired pre-payload lease."""

    while True:
        claim = store.claim_attempt(
            fingerprint=fingerprint,
            payer=payer,
            recovery_scope=recovery_scope,
        )
        if claim.is_owner or claim.attempt.payment_payload is not None:
            return claim
        await asyncio.sleep(0.01)


def _validate_payload_for_challenge(
    payload: PaymentPayload,
    challenge: PaymentRequired,
) -> None:
    """Bind recovery to the merchant's current canonical challenge."""

    if payload.x402_version != 2:
        raise ValueError("Stored payment attempt is not canonical x402 v2")
    if payload.resource != challenge.resource:
        raise ValueError(
            "Stored payment attempt resource does not match the current challenge"
        )
    if payload.accepted not in challenge.accepts:
        raise ValueError(
            "Stored accepted requirements do not match the current challenge"
        )


def _request_fingerprint(
    *,
    payer: str,
    url: str,
    method: str,
    headers: dict[str, str],
    content: bytes | None,
) -> str:
    canonical = {
        "payer": payer,
        "bodySha256": hashlib.sha256(content or b"").hexdigest(),
        "headers": sorted(
            (key.lower(), value)
            for key, value in headers.items()
            if key.lower()
            not in {
                PAYMENT_SIGNATURE_HEADER.lower(),
                PAYMENT_RESPONSE_HEADER.lower(),
            }
        ),
        "method": method.upper(),
        "url": str(httpx.URL(url)),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview(
    challenge: PaymentRequired | None,
    network: str,
    limit: XRPLAssetSpendLimit | None,
) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "mode": "dry_run",
        "x402Version": 2,
        "challengePresent": challenge is not None,
    }
    if challenge is None:
        return preview
    if limit is not None:
        selected = select_payment_option(
            challenge,
            network=network,
            asset=limit.asset,
            issuer=limit.issuer,
        )
        permitted = Decimal(selected.amount) <= Decimal(limit.max_amount)
    else:
        defaults: list[PaymentRequirements] = []
        for item in challenge.accepts:
            default = find_default_asset(item.asset, network)
            if (
                str(item.network) == network
                and default is not None
                and (item.extra or {}).get("issuer")
                == default.get("issuer")
            ):
                defaults.append(item)
        if not defaults:
            raise ValueError("challenge has no recognized default XRPL pegged asset")
        selected = defaults[0]
        permitted = Decimal(selected.amount) <= Decimal("1")
    preview["accepted"] = selected.model_dump(by_alias=True, exclude_none=True)
    preview["wouldPay"] = permitted
    return preview


def format_pay_result(result: PayResult) -> str:
    if result.preview is not None:
        return json.dumps(result.preview, indent=2, sort_keys=True)
    if result.text.strip():
        return result.text
    return json.dumps(
        {
            "statusCode": result.status_code,
            "paid": result.paid,
            "pending": result.pending,
            "receipt": (
                result.receipt.model_dump(mode="json", by_alias=True)
                if result.receipt
                else None
            ),
        },
        indent=2,
        sort_keys=True,
    )


def get_receipts(
    limit: int = 10, *, store: ReceiptStore | None = None
) -> list[dict[str, Any]]:
    return [
        receipt.model_dump(mode="json", by_alias=True)
        for receipt in (store or ReceiptStore()).list(limit=limit)
    ]


def budget_status(
    *,
    asset: str,
    issuer: str | None = None,
    network: str | None = None,
    max_spend: str | Decimal | None = None,
    store: ReceiptStore | None = None,
) -> dict[str, str | None]:
    resolved_network = network or os.getenv("XRPL_NETWORK") or DEFAULT_NETWORK
    return (store or ReceiptStore()).budget_summary(
        network=resolved_network,
        asset=normalize_currency_code(asset),
        issuer=issuer,
        max_spend=Decimal(str(max_spend)) if max_spend is not None else None,
    )

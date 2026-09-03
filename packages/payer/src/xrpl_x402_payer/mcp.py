from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import inspect
import json
from typing import Any

from x402 import x402Client
from x402.extensions.payment_identifier import extract_payment_identifier
from x402.mcp import (
    create_x402_mcp_client,
    x402MCPClient,
)
from x402.mcp.client_async import convert_mcp_result
from x402.mcp.types import PaymentRequiredContext, PaymentRequiredHookResult
from x402.schemas import PaymentPayload, PaymentRequired, SettleResponse

from xrpl_x402_client import (
    XRPLAssetSpendLimit,
    XRPLPaymentSigner,
    apply_xrpl_spend_limits,
    register_exact_xrpl_client,
)
from xrpl_x402_payer.journal import AttemptClaim
from xrpl_x402_payer.payer import (
    _claim_or_wait,
    _pending_settlement,
    _validate_payload_for_challenge,
)
from xrpl_x402_payer.payer import budget_status as get_budget_status
from xrpl_x402_payer.payer import format_pay_result, get_receipts, pay_with_x402
from xrpl_x402_payer.proxy import proxy_manager
from xrpl_x402_payer.receipts import ReceiptRecord, ReceiptStore

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - optional extra
    FastMCP = None


def build_xrpl_payment_client(
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    asset_limits: list[XRPLAssetSpendLimit] | None = None,
) -> x402Client:
    """Build the official x402 client with the XRPL mechanism and spend controls."""

    client = register_exact_xrpl_client(
        x402Client(), signer, network or signer.network
    )
    if asset_limits:
        apply_xrpl_spend_limits(client, asset_limits)
    else:
        client.set_spend_controls({"max_amount_per_payment": "$1"})
    return client


def wrap_mcp_client_with_xrpl_payment(
    mcp_client: Any,
    signer: XRPLPaymentSigner,
    *,
    network: str | None = None,
    asset_limits: list[XRPLAssetSpendLimit] | None = None,
    store: ReceiptStore | None = None,
    recovery_scope: str,
) -> x402MCPClient:
    """Wrap MCP with durable recovery scoped to one endpoint/auth principal."""

    return XRPLMCPClient(
        mcp_client,
        build_xrpl_payment_client(
            signer, network=network, asset_limits=asset_limits
        ),
        payer_account=signer.classic_address,
        recovery_scope=recovery_scope,
        store=store,
    )


@dataclass(slots=True)
class _MCPAttemptContext:
    client_id: int
    fingerprint: str
    claim: AttemptClaim
    challenge: PaymentRequired


_CURRENT_ATTEMPT: ContextVar[_MCPAttemptContext | None] = ContextVar(
    "xrpl_x402_mcp_attempt", default=None
)


class XRPLMCPClient(x402MCPClient):
    """Official MCP lifecycle with receipts and identical pending retries."""

    def __init__(
        self,
        mcp_client: Any,
        payment_client: Any,
        *,
        payer_account: str,
        recovery_scope: str,
        store: ReceiptStore | None = None,
    ) -> None:
        super().__init__(mcp_client, payment_client)
        if not payer_account.strip():
            raise ValueError("payer_account must be non-empty")
        if not recovery_scope.strip():
            raise ValueError("recovery_scope must be non-empty")
        self._payer_account = payer_account
        # Only the digest is persisted or included in the request fingerprint.
        # Callers must pass a stable endpoint/auth-principal label, never a token.
        self._recovery_scope = hashlib.sha256(
            recovery_scope.encode("utf-8")
        ).hexdigest()
        self._receipt_store = store or ReceiptStore()
        self.on_payment_required(self._resume_pending)

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        token = _CURRENT_ATTEMPT.set(None)
        try:
            return await super().call_tool(name, args, **kwargs)
        except BaseException:
            current = _CURRENT_ATTEMPT.get()
            if current is not None and current.client_id == id(self):
                self._receipt_store.release_claim(
                    current.fingerprint, current.claim.owner_token
                )
            raise
        finally:
            _CURRENT_ATTEMPT.reset(token)

    async def _resume_pending(
        self, context: PaymentRequiredContext
    ) -> PaymentRequiredHookResult | None:
        fingerprint = _mcp_request_fingerprint(
            payer=self._payer_account,
            recovery_scope=self._recovery_scope,
            name=context.tool_name,
            args=context.arguments,
            resource=context.payment_required.resource,
        )
        claim = await _claim_or_wait(
            self._receipt_store,
            fingerprint=fingerprint,
            payer=self._payer_account,
            recovery_scope=self._recovery_scope,
        )
        current = _MCPAttemptContext(
            client_id=id(self),
            fingerprint=fingerprint,
            claim=claim,
            challenge=context.payment_required,
        )
        _CURRENT_ATTEMPT.set(current)
        payload = claim.attempt.payment_payload
        if payload is None:
            return None
        _validate_payload_for_challenge(payload, context.payment_required)
        return PaymentRequiredHookResult(payment=payload)

    async def call_tool_with_payment(
        self,
        name: str,
        args: dict[str, Any],
        payload: PaymentPayload,
        **kwargs: Any,
    ) -> Any:
        current = _CURRENT_ATTEMPT.get()
        if current is not None and current.client_id == id(self):
            expected = _mcp_request_fingerprint(
                payer=self._payer_account,
                recovery_scope=self._recovery_scope,
                name=name,
                args=args,
                resource=current.challenge.resource,
            )
            if expected != current.fingerprint:
                raise ValueError("MCP payment attempt context mismatch")
            _validate_payload_for_challenge(payload, current.challenge)
            claim = current.claim
            fingerprint = current.fingerprint
        else:
            # Explicit-payload calls still participate in the same atomic
            # claim. The supplied payload is the caller's current challenge.
            fingerprint = _mcp_request_fingerprint(
                payer=self._payer_account,
                recovery_scope=self._recovery_scope,
                name=name,
                args=args,
                resource=payload.resource,
            )
            claim = await _claim_or_wait(
                self._receipt_store,
                fingerprint=fingerprint,
                payer=self._payer_account,
                recovery_scope=self._recovery_scope,
            )

        winner = claim.attempt.payment_payload
        if winner is None:
            attempt = self._receipt_store.persist_attempt_payload(
                fingerprint=fingerprint,
                owner_token=claim.owner_token,
                payment_payload=payload,
                payment_identifier=extract_payment_identifier(payload),
            )
            winner = attempt.payment_payload
        elif winner != payload:
            raise ValueError(
                "A different signed payload is already bound to this MCP request"
            )
        if winner is None:  # pragma: no cover - model invariant
            raise RuntimeError("Persisted MCP payment payload disappeared")

        # Persist happens before the upstream call boundary attaches _meta and
        # dispatches the paid tool invocation.
        try:
            result = await super().call_tool_with_payment(
                name, args, winner, **kwargs
            )
        except BaseException:
            self._record_mcp_outcome(
                name=name,
                payload=winner,
                fingerprint=fingerprint,
                settlement=_pending_settlement(winner),
                status_code=0,
            )
            raise
        settlement = result.payment_response or _pending_settlement(winner)
        if result.payment_response is None:
            result.payment_response = settlement
        if not settlement.success:
            result.content = []
            result.is_error = True
            result.raw_result = None
        self._record_mcp_outcome(
            name=name,
            payload=winner,
            fingerprint=fingerprint,
            settlement=settlement,
            status_code=200 if settlement.success else 402,
        )
        return result

    def _record_mcp_outcome(
        self,
        *,
        name: str,
        payload: PaymentPayload,
        fingerprint: str,
        settlement: SettleResponse,
        status_code: int,
    ) -> None:
        pending = bool(
            not settlement.success
            and settlement.error_reason == "settlement_pending"
        )
        resource_url = (
            str(payload.resource.url)
            if payload.resource is not None
            else f"mcp://tool/{name}"
        )
        self._receipt_store.record_outcome(
            ReceiptRecord(
                created_at=datetime.now(UTC).isoformat(),
                url=resource_url,
                method=f"MCP:{name}",
                status_code=status_code,
                state=(
                    "paid"
                    if settlement.success
                    else "pending"
                    if pending
                    else "failed"
                ),
                settlement=settlement,
                accepted=payload.accepted,
                request_fingerprint=fingerprint,
            )
        )

    async def _call_mcp_tool(
        self, params: dict[str, Any], **kwargs: Any
    ) -> Any:
        """Support both upstream param-style clients and raw MCP sessions."""

        call_tool = self._mcp_client.call_tool
        parameters = inspect.signature(call_tool).parameters
        if "arguments" not in parameters:
            return await super()._call_mcp_tool(params, **kwargs)
        raw = await call_tool(
            name=params["name"],
            arguments=params.get("arguments", {}),
            meta=params.get("_meta"),
            **kwargs,
        )
        converted = convert_mcp_result(raw)
        if isinstance(getattr(raw, "meta", None), dict):
            converted.meta = raw.meta
        return converted


def _mcp_request_fingerprint(
    *,
    payer: str,
    recovery_scope: str,
    name: str,
    args: dict[str, Any],
    resource: Any,
) -> str:
    resource_value = (
        resource.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(resource, "model_dump")
        else resource
    )
    encoded = json.dumps(
        {
            "arguments": args,
            "payer": payer,
            "recoveryScopeSha256": recovery_scope,
            "resource": resource_value,
            "tool": name,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def pay_url(
    url: str,
    asset: str | None = None,
    issuer: str | None = None,
    max_spend: str | None = None,
    dry_run: bool = False,
) -> str:
    """Pay for one URL. Non-default assets require issuer/cap authorization."""

    result = await pay_with_x402(
        url=url,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
    )
    return format_pay_result(result)


async def list_receipts(limit: int = 10) -> str:
    """List standard x402 settlement receipts and their terminal state."""

    receipts = get_receipts(limit=limit)
    if not receipts:
        return "No receipts recorded yet."
    return json.dumps(receipts, indent=2, sort_keys=True)


async def budget_status(
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: str | None = None,
) -> str:
    """Show paid-only local spend totals for one XRPL asset."""

    summary = get_budget_status(
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
    )
    return json.dumps(summary, indent=2, sort_keys=True)


async def proxy_mode(
    target_base_url: str,
    local_port: int = 8787,
    asset: str | None = None,
    issuer: str | None = None,
    max_spend: str | None = None,
    dry_run: bool = False,
) -> str:
    """Start or reuse the local v2 x402 payer forward proxy."""

    bind_url = proxy_manager.start(
        target_base_url=target_base_url,
        port=local_port,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
    )
    return f"Proxy ready at {bind_url} -> {target_base_url}"


if FastMCP is not None:
    mcp = FastMCP(
        name="xrpl-x402-payer",
        instructions=(
            "Pay canonical x402 v2 XRPL resources. Default payment selection is "
            "limited to recognized pegged assets and USD 1 per payment. XRP, USDC, "
            "and custom IOUs require an explicit asset, issuer where applicable, and cap."
        ),
    )
    mcp.tool()(pay_url)
    mcp.tool()(list_receipts)
    mcp.tool()(budget_status)
    mcp.tool()(proxy_mode)
else:
    mcp = None


def main() -> None:
    if mcp is None:
        raise RuntimeError(
            'FastMCP is not installed. Reinstall with: pip install "xrpl-x402-payer[mcp]"'
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = [
    "XRPLMCPClient",
    "build_xrpl_payment_client",
    "create_x402_mcp_client",
    "wrap_mcp_client_with_xrpl_payment",
]

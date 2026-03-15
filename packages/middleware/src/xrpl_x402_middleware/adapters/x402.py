from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

import httpx

from xrpl_x402_core import (
    FacilitatorSupportedResponse,
    FacilitatorVerifyResponse,
    PaymentPayload as CorePaymentPayload,
    XRPLAsset,
    canonical_asset_identifier,
)

if TYPE_CHECKING:
    from x402 import x402ResourceServer, x402ResourceServerSync
    from x402.schemas import AssetAmount, PaymentPayload, PaymentRequirements, SettleResponse, SupportedKind, SupportedResponse, VerifyResponse


ServerT = TypeVar("ServerT", "x402ResourceServer", "x402ResourceServerSync")


class ExactXRPLServerScheme:
    scheme = "exact"

    def parse_price(self, price: Any, network: str) -> "AssetAmount":
        from x402.schemas import AssetAmount

        if isinstance(price, AssetAmount):
            asset = self._normalize_asset_identifier(price.asset)
            return AssetAmount(amount=str(price.amount), asset=asset, extra=price.extra)

        if isinstance(price, dict):
            parsed = AssetAmount.model_validate(price)
            return self.parse_price(parsed, network)

        if isinstance(price, float) and not price.is_integer():
            raise ValueError("XRPL exact prices must be explicit drops or AssetAmount values")

        rendered = str(price).strip()
        if rendered.startswith("$"):
            raise ValueError("XRPL exact prices do not support USD-denominated prices")
        if not rendered:
            raise ValueError("XRPL exact prices cannot be empty")

        amount = str(int(rendered))
        return AssetAmount(amount=amount, asset="XRP:native")

    def enhance_payment_requirements(
        self,
        requirements: "PaymentRequirements",
        supported_kind: "SupportedKind",
        extensions: list[str],
    ) -> "PaymentRequirements":
        merged_extra = dict(requirements.extra)
        if supported_kind.extra:
            merged_extra.update(supported_kind.extra)
        if extensions:
            merged_extra.setdefault("extensions", list(extensions))
        return requirements.model_copy(update={"extra": merged_extra})

    @staticmethod
    def _normalize_asset_identifier(identifier: str) -> str:
        asset = XRPLAsset.model_validate(_asset_identifier_to_model(identifier))
        return canonical_asset_identifier(asset)


class XRPLX402FacilitatorClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout: float = 10.0,
        async_client: httpx.AsyncClient | None = None,
        sync_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._timeout = timeout
        self._async_client = async_client
        self._sync_client = sync_client
        self._owns_client = async_client is None

    async def aclose(self) -> None:
        if self._async_client is not None and self._owns_client:
            await self._async_client.aclose()

    def get_supported(self) -> "SupportedResponse":
        from x402.schemas import SupportedKind, SupportedResponse

        if self._sync_client is not None:
            response = self._sync_client.get(f"{self._base_url}/supported")
            response.raise_for_status()
            supported = FacilitatorSupportedResponse.model_validate(response.json())
        else:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                response = client.get(f"{self._base_url}/supported")
                response.raise_for_status()
                supported = FacilitatorSupportedResponse.model_validate(response.json())

        assets = [canonical_asset_identifier(asset) for asset in supported.assets]
        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402_version=2,
                    scheme="exact",
                    network=supported.network,
                    extra={"xrpl": {"assets": assets, "settlementMode": supported.settlement_mode}},
                )
            ]
        )

    async def verify(
        self,
        payload: "PaymentPayload",
        requirements: "PaymentRequirements",
    ) -> "VerifyResponse":
        from x402.schemas import VerifyResponse

        try:
            verify_response = await self._post_payment_stage("verify", payload, requirements)
        except _PaymentRejected as exc:
            return VerifyResponse(
                is_valid=False,
                invalid_reason=exc.reason,
                invalid_message=exc.message,
            )

        return VerifyResponse(
            is_valid=verify_response.valid,
            payer=verify_response.payer,
        )

    async def settle(
        self,
        payload: "PaymentPayload",
        requirements: "PaymentRequirements",
    ) -> "SettleResponse":
        from x402.schemas import SettleResponse

        try:
            verify_response = await self._post_payment_stage("settle", payload, requirements)
        except _PaymentRejected as exc:
            return SettleResponse(
                success=False,
                error_reason=exc.reason,
                error_message=exc.message,
                transaction="",
                network=requirements.network,
            )

        transaction = verify_response.get("tx_hash", "")
        payer = verify_response.get("payer")
        return SettleResponse(
            success=True,
            transaction=transaction,
            network=requirements.network,
            payer=payer,
        )

    async def _post_payment_stage(
        self,
        stage: str,
        payload: "PaymentPayload",
        requirements: "PaymentRequirements",
    ) -> dict[str, Any] | FacilitatorVerifyResponse:
        core_payload = self._to_core_payload(payload, requirements)
        request_body = {
            "signed_tx_blob": core_payload.payload.signed_tx_blob,
        }
        if core_payload.payload.invoice_id is not None:
            request_body["invoice_id"] = core_payload.payload.invoice_id

        client = self._async_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)
            self._async_client = client

        response = await client.post(
            f"{self._base_url}/{stage}",
            headers={"Authorization": f"Bearer {self._bearer_token}"},
            json=request_body,
        )

        if response.status_code in {401, 402}:
            raise _PaymentRejected(
                reason=f"{stage}_rejected",
                message=_extract_detail(response),
            )
        response.raise_for_status()

        body = response.json()
        if stage == "verify":
            return FacilitatorVerifyResponse.model_validate(body)
        return body

    @staticmethod
    def _to_core_payload(
        payload: "PaymentPayload",
        requirements: "PaymentRequirements",
    ) -> CorePaymentPayload:
        expected_asset = ExactXRPLServerScheme._normalize_asset_identifier(requirements.asset)
        if payload.accepted.asset != expected_asset:
            raise ValueError("x402 payload asset does not match XRPL requirements")
        return CorePaymentPayload(
            network=requirements.network,
            payload=payload.payload,
        )


def register_exact_xrpl_server(
    server: ServerT,
    networks: str | list[str] | None = None,
) -> ServerT:
    scheme = ExactXRPLServerScheme()
    if networks:
        if isinstance(networks, str):
            networks = [networks]
        for network in networks:
            server.register(network, scheme)
    else:
        server.register("xrpl:*", scheme)
    return server


class _PaymentRejected(Exception):
    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or "unknown facilitator error"

    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return response.text.strip() or "unknown facilitator error"


def _asset_identifier_to_model(identifier: str) -> dict[str, str]:
    code, separator, issuer = identifier.partition(":")
    if not separator:
        raise ValueError("Asset identifier must use CODE:ISSUER or CODE:native")
    if issuer == "native":
        return {"code": code}
    return {"code": code, "issuer": issuer}

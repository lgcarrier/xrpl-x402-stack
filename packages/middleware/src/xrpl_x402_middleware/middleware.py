from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Protocol

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xrpl_x402_core import PaymentPayload, PaymentRequired, PaymentResponse, XRPLAmount, XRPLAsset, XRPLPaymentOption
from xrpl_x402_middleware.client import (
    FacilitatorSettleResponse,
    FacilitatorSupported,
    FacilitatorVerifyResponse,
    XRPLFacilitatorClient,
)
from xrpl_x402_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    FacilitatorTransportError,
    InvalidPaymentHeaderError,
    RouteConfigurationError,
)
from xrpl_x402_middleware.types import RouteConfig
from xrpl_x402_middleware.utils import (
    canonical_asset_identifier,
    decode_model_from_base64,
    encode_model_to_base64,
    payment_option_matches,
)

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


class FacilitatorClientProtocol(Protocol):
    async def startup(self) -> None:
        ...

    async def aclose(self) -> None:
        ...

    async def get_supported(self, *, force_refresh: bool = False) -> FacilitatorSupported:
        ...

    async def verify_payment(
        self,
        *,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> FacilitatorVerifyResponse:
        ...

    async def settle_payment(
        self,
        *,
        signed_tx_blob: str,
        invoice_id: str | None = None,
    ) -> FacilitatorSettleResponse:
        ...


class PaymentMiddlewareASGI:
    def __init__(
        self,
        app: ASGIApp,
        *,
        route_configs: Mapping[str, RouteConfig | dict[str, Any]],
        client_factory: Callable[[str, str], FacilitatorClientProtocol] | None = None,
    ) -> None:
        self.app = app
        self._client_factory = client_factory or self._default_client_factory
        self._startup_lock = asyncio.Lock()
        self._started = False
        self._routes: dict[tuple[str, str], RouteConfig] = {}
        self._clients: dict[tuple[str, str], FacilitatorClientProtocol] = {}

        for route_key, route_config in route_configs.items():
            method, path = self._parse_route_key(route_key)
            self._routes[(method, path)] = (
                route_config
                if isinstance(route_config, RouteConfig)
                else RouteConfig.model_validate(route_config)
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        await self.startup()

        route_config = self._routes.get((scope["method"].upper(), scope["path"]))
        if route_config is None:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        try:
            payment_payload = self._decode_payment_header(headers)
        except InvalidPaymentHeaderError as exc:
            await self._send_challenge(
                route_config,
                send=send,
                receive=receive,
                scope=scope,
                error=str(exc),
            )
            return

        candidate_options = self._find_options_for_payload(route_config, payment_payload.network)
        if not candidate_options:
            await self._send_challenge(
                route_config,
                send=send,
                receive=receive,
                scope=scope,
                error="Payment network is not accepted for this route",
            )
            return

        client = self._get_client(route_config)
        try:
            verification = await client.verify_payment(
                signed_tx_blob=payment_payload.payload.signed_tx_blob,
                invoice_id=payment_payload.payload.invoice_id,
            )
        except FacilitatorPaymentError as exc:
            await self._send_challenge(
                route_config,
                send=send,
                receive=receive,
                scope=scope,
                error=exc.detail,
            )
            return
        except FacilitatorTransportError as exc:
            await self._send_error(send, receive, scope, 503, str(exc))
            return
        except FacilitatorProtocolError as exc:
            await self._send_error(send, receive, scope, 502, str(exc))
            return

        matched_option = next(
            (
                option
                for option in candidate_options
                if payment_option_matches(
                    option,
                    destination=verification.destination,
                    asset=verification.asset,
                    amount=verification.amount_details,
                )
            ),
            None,
        )
        if matched_option is None:
            await self._send_challenge(
                route_config,
                send=send,
                receive=receive,
                scope=scope,
                error="Submitted payment does not satisfy this route",
            )
            return

        try:
            settlement = await client.settle_payment(
                signed_tx_blob=payment_payload.payload.signed_tx_blob,
                invoice_id=payment_payload.payload.invoice_id,
            )
        except FacilitatorTransportError as exc:
            await self._send_error(send, receive, scope, 503, str(exc))
            return
        except (FacilitatorProtocolError, FacilitatorPaymentError) as exc:
            detail = exc.detail if isinstance(exc, FacilitatorPaymentError) else str(exc)
            await self._send_error(send, receive, scope, 502, f"Facilitator settlement failed: {detail}")
            return

        payment_response = PaymentResponse(
            network=payment_payload.network,
            payer=verification.payer,
            pay_to=verification.destination,
            invoice_id=verification.invoice_id,
            tx_hash=settlement.tx_hash,
            settlement_status=settlement.status,
            asset=verification.asset,
            amount=verification.amount_details,
        )
        scope.setdefault("state", {})
        scope["state"]["x402_payment"] = payment_response

        async def send_with_payment_response(message: Message) -> None:
            if message["type"] == "http.response.start" and 200 <= message["status"] < 400:
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers[PAYMENT_RESPONSE_HEADER] = encode_model_to_base64(payment_response)
            await send(message)

        await self.app(scope, receive, send_with_payment_response)

    async def startup(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            for route_key, route_config in self._routes.items():
                client = self._get_client(route_config)
                await client.startup()
                supported = await client.get_supported()
                self._validate_route_support(route_key, route_config, supported)
            self._started = True

    async def shutdown(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._started = False

    @staticmethod
    def _parse_route_key(route_key: str) -> tuple[str, str]:
        method, separator, path = route_key.partition(" ")
        if not separator or not path.startswith("/"):
            raise RouteConfigurationError(
                f"Route key '{route_key}' must use the format 'METHOD /path'"
            )
        return method.upper(), path

    @staticmethod
    def _default_client_factory(
        facilitator_url: str,
        bearer_token: str,
    ) -> FacilitatorClientProtocol:
        return XRPLFacilitatorClient(base_url=facilitator_url, bearer_token=bearer_token)

    def _get_client(self, route_config: RouteConfig) -> FacilitatorClientProtocol:
        client_key = (route_config.facilitator_url, route_config.bearer_token)
        client = self._clients.get(client_key)
        if client is None:
            client = self._client_factory(*client_key)
            self._clients[client_key] = client
        return client

    @staticmethod
    def _decode_payment_header(headers: Headers) -> PaymentPayload:
        raw_header = headers.get(PAYMENT_SIGNATURE_HEADER)
        if raw_header is None:
            raise InvalidPaymentHeaderError("Missing PAYMENT-SIGNATURE header")
        return decode_model_from_base64(raw_header, PaymentPayload)

    @staticmethod
    def _find_options_for_payload(
        route_config: RouteConfig,
        network: str,
    ) -> list[XRPLPaymentOption]:
        return [option for option in route_config.accepts if option.network == network]

    @staticmethod
    def _validate_route_support(
        route_key: tuple[str, str],
        route_config: RouteConfig,
        supported: FacilitatorSupported,
    ) -> None:
        for option in route_config.accepts:
            if option.network != supported.network:
                method, path = route_key
                raise RouteConfigurationError(
                    f"{method} {path} expects {option.network}, but facilitator supports "
                    f"{supported.network}"
                )
            if canonical_asset_identifier(option.asset) not in {
                canonical_asset_identifier(asset) for asset in supported.assets
            }:
                method, path = route_key
                raise RouteConfigurationError(
                    f"{method} {path} uses unsupported asset "
                    f"{canonical_asset_identifier(option.asset)}"
                )

    async def _send_challenge(
        self,
        route_config: RouteConfig,
        *,
        send: Send,
        receive: Receive,
        scope: Scope,
        error: str,
    ) -> None:
        challenge = PaymentRequired(error=error, accepts=route_config.accepts)
        response = JSONResponse(
            status_code=402,
            content=challenge.model_dump(by_alias=True, exclude_none=True),
            headers={PAYMENT_REQUIRED_HEADER: encode_model_to_base64(challenge)},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _send_error(
        send: Send,
        receive: Receive,
        scope: Scope,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)


def require_payment(
    *,
    facilitator_url: str,
    bearer_token: str,
    pay_to: str,
    network: str,
    xrp_drops: int | None = None,
    amount: str | None = None,
    asset_code: str = "XRP",
    asset_issuer: str | None = None,
    description: str | None = None,
    expires_at: int | None = None,
    mime_type: str = "application/json",
) -> RouteConfig:
    if xrp_drops is None and amount is None:
        raise RouteConfigurationError("require_payment needs xrp_drops or amount")
    if xrp_drops is not None and amount is not None:
        raise RouteConfigurationError("require_payment accepts either xrp_drops or amount")

    asset = XRPLAsset(code=asset_code, issuer=asset_issuer)
    if asset.code == "XRP":
        if xrp_drops is None:
            raise RouteConfigurationError("XRP payments must use xrp_drops")
        xrpl_amount = XRPLAmount(value=str(xrp_drops), unit="drops", drops=xrp_drops)
    else:
        if amount is None:
            raise RouteConfigurationError("Issued-asset payments must use amount")
        xrpl_amount = XRPLAmount(value=amount, unit="issued")

    option = XRPLPaymentOption(
        network=network,
        pay_to=pay_to,
        max_amount_required=xrpl_amount.value,
        asset=asset,
        amount=xrpl_amount,
        description=description,
        mime_type=mime_type,
        expires_at=expires_at,
    )
    return RouteConfig(
        facilitator_url=facilitator_url,
        bearer_token=bearer_token,
        accepts=[option],
        description=description,
        mime_type=mime_type,
    )

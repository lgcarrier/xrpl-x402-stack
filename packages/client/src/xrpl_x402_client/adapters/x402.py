from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from xrpl_x402_client.signer import XRPLPaymentSigner

if TYPE_CHECKING:
    from x402 import x402Client, x402ClientSync
    from x402.schemas import PaymentRequirements


ClientT = TypeVar("ClientT", "x402Client", "x402ClientSync")


class ExactXRPLClientScheme:
    scheme = "exact"

    def __init__(self, signer: XRPLPaymentSigner) -> None:
        self._signer = signer

    def create_payment_payload(self, requirements: "PaymentRequirements") -> dict[str, Any]:
        payload = self._signer.build_x402_payload(
            network=requirements.network,
            asset_identifier=requirements.asset,
            amount=requirements.amount,
            pay_to=requirements.pay_to,
        )
        return payload.payload.model_dump(by_alias=True, exclude_none=True)


def register_exact_xrpl_client(
    client: ClientT,
    signer: XRPLPaymentSigner,
    networks: str | list[str] | None = None,
    policies: list | None = None,
) -> ClientT:
    scheme = ExactXRPLClientScheme(signer)
    if networks:
        if isinstance(networks, str):
            networks = [networks]
        for network in networks:
            client.register(network, scheme)
    else:
        client.register("xrpl:*", scheme)

    if policies:
        for policy in policies:
            client.register_policy(policy)
    return client

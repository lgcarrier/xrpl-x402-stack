class XRPLX402MiddlewareError(Exception):
    """Base exception for xrpl_x402_middleware errors."""


class RouteConfigurationError(XRPLX402MiddlewareError):
    """Raised when middleware route configuration is invalid."""


class InvalidPaymentHeaderError(XRPLX402MiddlewareError):
    """Raised when a payment header cannot be decoded or validated."""


class FacilitatorError(XRPLX402MiddlewareError):
    """Base exception for facilitator client failures."""


class FacilitatorTransportError(FacilitatorError):
    """Raised when the facilitator cannot be reached or returns 5xx."""


class FacilitatorProtocolError(FacilitatorError):
    """Raised when the facilitator returns an unexpected response shape."""


class FacilitatorPaymentError(FacilitatorError):
    """Raised when the facilitator rejects a payment during verify/settle."""

    def __init__(self, stage: str, status_code: int, detail: str) -> None:
        self.stage = stage
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{stage} failed with {status_code}: {detail}")

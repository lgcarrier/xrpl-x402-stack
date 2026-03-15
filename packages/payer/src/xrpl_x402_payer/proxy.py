from __future__ import annotations

import threading
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
import uvicorn

from xrpl_x402_payer.payer import XRPLPayer, build_signer_from_env
from xrpl_x402_payer.receipts import ReceiptStore

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def create_proxy_app(
    *,
    target_base_url: str,
    amount: float = 0.001,
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: float | None = None,
    dry_run: bool = False,
    transport=None,
    store: ReceiptStore | None = None,
    payer: XRPLPayer | None = None,
) -> Starlette:
    active_payer = payer or XRPLPayer(
        None if dry_run else build_signer_from_env(),
        store=store,
    )
    normalized_target = target_base_url.rstrip("/")

    async def proxy(request: Request) -> Response:
        path = request.path_params.get("path", "")
        target_url = normalized_target
        if path:
            target_url = f"{target_url}/{path.lstrip('/')}"
        if request.url.query:
            target_url = f"{target_url}?{request.url.query}"

        body = await request.body()
        forwarded_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        result = await active_payer.pay(
            url=target_url,
            method=request.method,
            headers=forwarded_headers,
            content=body or None,
            amount=amount,
            asset=asset,
            issuer=issuer,
            max_spend=max_spend,
            dry_run=dry_run,
            transport=transport,
        )
        response_headers = {
            key: value
            for key, value in result.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=response_headers,
        )

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                endpoint=proxy,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            )
        ]
    )


def run_proxy(
    *,
    target_base_url: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    amount: float = 0.001,
    asset: str = "XRP",
    issuer: str | None = None,
    max_spend: float | None = None,
    dry_run: bool = False,
) -> None:
    app = create_proxy_app(
        target_base_url=target_base_url,
        amount=amount,
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
        dry_run=dry_run,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


class ProxyManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._bind_url: str | None = None
        self._target_base_url: str | None = None

    def start(
        self,
        *,
        target_base_url: str,
        host: str = "127.0.0.1",
        port: int = 8787,
        amount: float = 0.001,
        asset: str = "XRP",
        issuer: str | None = None,
        max_spend: float | None = None,
        dry_run: bool = False,
    ) -> str:
        with self._lock:
            bind_url = f"http://{host}:{port}"
            if self._server is not None:
                if self._bind_url == bind_url and self._target_base_url == target_base_url:
                    return bind_url
                raise RuntimeError(
                    "Proxy is already running with a different configuration. Restart the MCP server to change it."
                )

            app = create_proxy_app(
                target_base_url=target_base_url,
                amount=amount,
                asset=asset,
                issuer=issuer,
                max_spend=max_spend,
                dry_run=dry_run,
            )
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()

            for _ in range(100):
                if getattr(server, "started", False):
                    self._server = server
                    self._thread = thread
                    self._bind_url = bind_url
                    self._target_base_url = target_base_url
                    return bind_url
                time.sleep(0.05)

            raise RuntimeError("Proxy server failed to start")


proxy_manager = ProxyManager()

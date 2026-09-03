from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient
from x402.extensions.payment_identifier import (
    append_payment_identifier_to_extensions,
    declare_payment_identifier_extension,
)
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.mcp import (
    MCP_PAYMENT_META_KEY,
    MCP_PAYMENT_RESPONSE_META_KEY,
    x402MCPClient,
)
from x402.schemas import (
    PaymentPayload,
    PaymentRequirements,
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from xrpl_x402_middleware import (
    PaymentMiddlewareASGI,
    RedisResourceResponseStore,
    ResourceInfo,
    XRPLPaymentContext,
    create_xrpl_mcp_payment_wrapper,
    require_payment,
)
from xrpl_x402_middleware.response_store import (
    AuthoritativeResource,
    StoredResourceResponse,
)

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


class FakeFacilitator:
    def __init__(
        self,
        *,
        valid: bool = True,
        verifications: list[VerifyResponse] | None = None,
        settlements: list[SettleResponse] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.valid = valid
        self.verifications = verifications
        self.settlements = settlements or [
            SettleResponse(
                success=True,
                payer="rPayer",
                transaction="A" * 64,
                network="xrpl:1",
                amount="1000",
            )
        ]
        self.events = events if events is not None else []
        self.verify_count = 0
        self.settle_count = 0

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402_version=2,
                    scheme="exact",
                    network="xrpl:1",
                    extra={
                        "areFeesSponsored": False,
                        "assetTransferMethods": ["sequence", "ticketSequence"],
                    },
                )
            ],
            extensions=["payment-identifier", "bazaar"],
        )

    async def verify(self, payload, requirements) -> VerifyResponse:  # type: ignore[no-untyped-def]
        self.events.append("verify")
        if self.verifications:
            result = self.verifications[
                min(self.verify_count, len(self.verifications) - 1)
            ]
        else:
            result = VerifyResponse(
                is_valid=self.valid,
                invalid_reason=None if self.valid else "invalid_payment",
                payer="rPayer" if self.valid else None,
            )
        self.verify_count += 1
        return result

    async def settle(self, payload, requirements) -> SettleResponse:  # type: ignore[no-untyped-def]
        self.events.append("settle")
        result = self.settlements[min(self.settle_count, len(self.settlements) - 1)]
        self.settle_count += 1
        return result


class AsyncStringRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_options: list[dict[str, Any]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if kwargs.get("nx") and key in self.values:
            self.set_options.append(kwargs)
            return False
        self.values[key] = value
        self.set_options.append(kwargs)
        return True


def route(*, method: str = "GET"):
    return require_payment(
        pay_to=DESTINATION,
        network="xrpl:1",
        xrp_drops=1000,
        resource=f"https://merchant.example/{method.lower()}",
        description="Protected resource",
        service_name="Merchant",
        tags=["premium"],
        icon_url="https://merchant.example/icon.png",
    )


def test_response_store_extends_ttl_past_payment_timeout() -> None:
    redis = AsyncStringRedis()
    store = RedisResourceResponseStore(
        redis,
        ttl_seconds=600,
        safety_margin_seconds=45,
    )
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=900,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    extensions = {
        "payment-identifier": declare_payment_identifier_extension(required=True)
    }
    append_payment_identifier_to_extensions(extensions, "pay_ttl_test_0001")
    authoritative = AuthoritativeResource(
        resource=ResourceInfo(url="https://merchant.example/unsafe"),
        identity="http:POST:https://merchant.example/unsafe",
    )
    payload = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        extensions=extensions,
    )
    cached = StoredResourceResponse(
        status_code=201,
        content_type="application/octet-stream",
        body=b"\x00\xffresult",
        headers={},
    )

    async def exercise_store() -> StoredResourceResponse | None:
        assert await store.bind(
            payload,
            accepted,
            authoritative_resource=authoritative,
        ) is True
        assert await store.bind(
            payload,
            accepted,
            authoritative_resource=authoritative,
        ) is False
        await store.put(
            payload,
            accepted,
            cached,
            authoritative_resource=authoritative,
        )
        restored = await store.get(
            payload,
            accepted,
            authoritative_resource=authoritative,
        )
        with pytest.raises(ValueError, match="different payment or resource context"):
            await store.get(
                payload,
                accepted,
                authoritative_resource=AuthoritativeResource(
                    resource=authoritative.resource,
                    identity="http:PUT:https://merchant.example/unsafe",
                ),
            )
        return restored

    restored = asyncio.run(exercise_store())

    assert redis.set_options[0] == {"ex": 945, "nx": True}
    assert redis.set_options[1] == {"ex": 945, "nx": True}
    assert redis.set_options[-1] == {"ex": 945}
    assert restored == cached
    record = json.loads(next(iter(redis.values.values())))
    assert record["bodyEncoding"] == "base64"


def test_mcp_wrapper_uses_canonical_challenge_and_requires_identifier() -> None:
    facilitator = FakeFacilitator()
    store = RedisResourceResponseStore(AsyncStringRedis())
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    wrapper = create_xrpl_mcp_payment_wrapper(
        facilitator,
        accepts=[accepted],
        resource=ResourceInfo(
            url="mcp://merchant/write_report",
            description="Write report",
            mime_type="application/json",
        ),
        non_idempotent=True,
        enable_bazaar=False,
        response_store=store,
    )

    @wrapper
    async def write_report(topic: str) -> dict[str, str]:
        return {"topic": topic}

    result = asyncio.run(write_report(topic="XRPL"))
    challenge = result.structuredContent
    assert challenge["x402Version"] == 2
    assert challenge["accepts"][0]["asset"] == "XRP"
    assert challenge["extensions"]["payment-identifier"]["info"]["required"] is True


def test_mcp_pending_retry_recovers_cached_result_without_repeating_handler() -> None:
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        error_message="awaiting validation",
        payer="rPayer",
        transaction="C" * 64,
        network="xrpl:1",
        amount="1000",
    )
    success = SettleResponse(
        success=True,
        payer="rPayer",
        transaction="C" * 64,
        network="xrpl:1",
        amount="1000",
    )
    facilitator = FakeFacilitator(
        verifications=[
            VerifyResponse(is_valid=True, payer="rPayer"),
            VerifyResponse(
                is_valid=False,
                invalid_reason=(
                    "invalid_exact_xrpl_payload_ticket_not_available"
                ),
                invalid_message="TicketSequence has already been consumed",
                payer="rPayer",
            ),
        ],
        settlements=[pending, pending, success],
    )
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    wrapper = create_xrpl_mcp_payment_wrapper(
        facilitator,
        accepts=[accepted],
        resource=ResourceInfo(
            url="mcp://merchant/write_report",
            description="Write report",
            mime_type="application/json",
        ),
        non_idempotent=True,
        enable_bazaar=False,
        response_store=store,
    )
    handled = {"count": 0}

    @wrapper
    async def write_report(topic: str) -> dict[str, Any]:
        handled["count"] += 1
        return {"topic": topic, "created": handled["count"]}

    extensions = {
        "payment-identifier": declare_payment_identifier_extension(required=True)
    }
    append_payment_identifier_to_extensions(extensions, "pay_mcp_retry_0001")
    payload = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        resource=ResourceInfo(
            url="mcp://merchant/write_report",
            description="Write report",
            mime_type="application/json",
        ),
        extensions=extensions,
    )
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            meta=SimpleNamespace(
                model_extra={
                    MCP_PAYMENT_META_KEY: payload.model_dump(
                        by_alias=True, exclude_none=True
                    )
                }
            )
        )
    )

    first = asyncio.run(write_report(topic="XRPL", ctx=context))
    second = asyncio.run(write_report(topic="XRPL", ctx=context))

    assert first.isError is True
    expected_pending = pending.model_dump(by_alias=True, exclude_none=True)
    pending_wire = first.structuredContent[MCP_PAYMENT_RESPONSE_META_KEY]
    assert pending_wire == expected_pending
    assert pending_wire["transaction"] == "C" * 64
    assert pending_wire["network"] == "xrpl:1"
    assert pending_wire["amount"] == "1000"
    assert pending_wire["errorReason"] == "settlement_pending"
    assert pending_wire["errorMessage"] == "awaiting validation"
    assert json.loads(first.content[0].text)[MCP_PAYMENT_RESPONSE_META_KEY] == (
        expected_pending
    )
    assert second.isError is False
    assert handled["count"] == 1
    assert facilitator.verify_count == 2
    assert facilitator.settle_count == 3
    assert second.meta[MCP_PAYMENT_RESPONSE_META_KEY]["success"] is True


def payment_header(client: TestClient, path: str) -> tuple[str, Any]:
    challenge_response = client.get(path) if path != "/unsafe" else client.post(path)
    challenge = decode_payment_required_header(
        challenge_response.headers[PAYMENT_REQUIRED_HEADER]
    )
    extensions = json.loads(json.dumps(challenge.extensions or {}))
    append_payment_identifier_to_extensions(
        extensions, "pay_test_identifier_0001"
    )
    payload = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=challenge.accepts[0],
        resource=challenge.resource,
        extensions=extensions,
    )
    return encode_payment_signature_header(payload), challenge


def test_http_payload_cannot_be_substituted_between_same_priced_routes() -> None:
    events: list[str] = []
    facilitator = FakeFacilitator(events=events)
    app = FastAPI()
    handled = {"a": 0, "b": 0}

    def protected(resource: str):
        return require_payment(
            pay_to=DESTINATION,
            network="xrpl:1",
            xrp_drops=1000,
            resource=resource,
            description="Protected resource",
        )

    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={
            "GET /a": protected("https://merchant.example/a"),
            "GET /b": protected("https://merchant.example/b"),
        },
        facilitator_client=facilitator,
    )

    @app.get("/a")
    async def resource_a() -> dict[str, str]:
        handled["a"] += 1
        return {"resource": "a"}

    @app.get("/b")
    async def resource_b() -> dict[str, str]:
        handled["b"] += 1
        return {"resource": "b"}

    with TestClient(app) as client:
        signature, challenge = payment_header(client, "/a")
        assert challenge.resource.url == "https://merchant.example/a"
        response = client.get(
            "/b", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert response.status_code == 402
    assert handled == {"a": 0, "b": 0}
    assert events == []
    assert facilitator.settle_count == 0


def test_mcp_payload_cannot_be_substituted_between_same_priced_tools() -> None:
    events: list[str] = []
    facilitator = FakeFacilitator(events=events)
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    shared_resource = ResourceInfo(
        url="mcp://merchant/shared",
        description="Shared paid tool resource",
        mime_type="application/json",
    )
    wrapper_a = create_xrpl_mcp_payment_wrapper(
        facilitator,
        accepts=[accepted],
        resource=shared_resource,
        non_idempotent=True,
        enable_bazaar=False,
        response_store=store,
    )
    wrapper_b = create_xrpl_mcp_payment_wrapper(
        facilitator,
        accepts=[accepted],
        resource=shared_resource,
        non_idempotent=True,
        enable_bazaar=False,
        response_store=store,
    )
    handled = {"a": 0, "b": 0}

    @wrapper_a
    async def tool_a(value: int) -> dict[str, Any]:
        handled["a"] += 1
        return {"tool": "a", "value": value}

    @wrapper_b
    async def tool_b(value: int) -> dict[str, Any]:
        handled["b"] += 1
        return {"tool": "b", "value": value}

    challenge_result = asyncio.run(tool_a(value=1))
    challenge = challenge_result.structuredContent
    payload = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        resource=ResourceInfo.model_validate(challenge["resource"]),
        extensions=json.loads(json.dumps(challenge.get("extensions") or {})),
    )
    append_payment_identifier_to_extensions(
        payload.extensions, "pay_mcp_tool_binding_0001"
    )
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            meta=SimpleNamespace(
                model_extra={
                    MCP_PAYMENT_META_KEY: payload.model_dump(
                        by_alias=True, exclude_none=True
                    )
                }
            )
        )
    )

    first = asyncio.run(tool_a(value=1, ctx=context))
    changed_arguments = asyncio.run(tool_a(value=2, ctx=context))
    changed_tool = asyncio.run(tool_b(value=1, ctx=context))

    assert first.isError is False
    assert changed_arguments.isError is True
    assert changed_tool.isError is True
    assert handled == {"a": 1, "b": 0}
    assert events == ["verify", "settle", "verify", "verify"]
    assert facilitator.settle_count == 1


def test_payment_identifier_binds_http_query_and_body_before_handler() -> None:
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        error_message="awaiting validation",
        payer="rPayer",
        transaction="F" * 64,
        network="xrpl:1",
        amount="1000",
    )
    events: list[str] = []
    facilitator = FakeFacilitator(
        verifications=[
            VerifyResponse(is_valid=True, payer="rPayer"),
            VerifyResponse(
                is_valid=False,
                invalid_reason=(
                    "invalid_exact_xrpl_payload_sequence_not_current"
                ),
                invalid_message="Account Sequence advanced after submission",
                payer="rPayer",
            ),
        ],
        settlements=[pending],
        events=events,
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    app = FastAPI()
    handled = {"count": 0}
    seen_bodies: list[bytes] = []
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"POST /input": route(method="POST")},
        facilitator_client=facilitator,
        response_store=store,
    )

    @app.post("/input")
    async def input_handler(request: Request) -> dict[str, int]:
        handled["count"] += 1
        seen_bodies.append(await request.body())
        return {"created": handled["count"]}

    with TestClient(app) as client:
        challenge_response = client.post("/input?mode=a", content=b"one")
        challenge = decode_payment_required_header(
            challenge_response.headers[PAYMENT_REQUIRED_HEADER]
        )
        extensions = json.loads(json.dumps(challenge.extensions or {}))
        append_payment_identifier_to_extensions(
            extensions, "pay_http_invocation_binding_0001"
        )
        payload = PaymentPayload(
            payload={"signedTxBlob": "AA"},
            accepted=challenge.accepts[0],
            resource=challenge.resource,
            extensions=extensions,
        )
        signature = encode_payment_signature_header(payload)
        first = client.post(
            "/input?mode=a",
            content=b"one",
            headers={PAYMENT_SIGNATURE_HEADER: signature},
        )
        changed_body = client.post(
            "/input?mode=a",
            content=b"two",
            headers={PAYMENT_SIGNATURE_HEADER: signature},
        )
        changed_query = client.post(
            "/input?mode=b",
            content=b"one",
            headers={PAYMENT_SIGNATURE_HEADER: signature},
        )

    assert first.status_code == 402
    assert changed_body.status_code == 409
    assert changed_query.status_code == 409
    assert changed_body.json()["error"].startswith(
        "payment-identifier was reused"
    )
    assert changed_query.json()["error"].startswith(
        "payment-identifier was reused"
    )
    assert handled["count"] == 1
    assert seen_bodies == [b"one"]
    assert events == ["verify", "settle", "settle", "verify", "verify"]
    assert facilitator.settle_count == 2


def test_payment_identifier_is_bound_to_http_method_before_handler() -> None:
    events: list[str] = []
    facilitator = FakeFacilitator(events=events)
    store = RedisResourceResponseStore(AsyncStringRedis())
    shared_resource = "https://merchant.example/same"
    app = FastAPI()
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={
            "POST /same": require_payment(
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                resource=shared_resource,
            ),
            "PUT /same": require_payment(
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                resource=shared_resource,
            ),
        },
        facilitator_client=facilitator,
        response_store=store,
    )
    started = Event()
    release = Event()
    handled = {"post": 0, "put": 0}

    @app.post("/same")
    async def post_same() -> dict[str, str]:
        handled["post"] += 1
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return {"method": "post"}

    @app.put("/same")
    async def put_same() -> dict[str, str]:
        handled["put"] += 1
        return {"method": "put"}

    with TestClient(app) as client:
        challenge_response = client.post("/same")
        challenge = decode_payment_required_header(
            challenge_response.headers[PAYMENT_REQUIRED_HEADER]
        )
        extensions = json.loads(json.dumps(challenge.extensions or {}))
        append_payment_identifier_to_extensions(
            extensions, "pay_http_method_binding_0001"
        )
        payload = PaymentPayload(
            payload={"signedTxBlob": "AA"},
            accepted=challenge.accepts[0],
            resource=challenge.resource,
            extensions=extensions,
        )
        signature = encode_payment_signature_header(payload)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                client.post,
                "/same",
                headers={PAYMENT_SIGNATURE_HEADER: signature},
            )
            assert started.wait(2)
            matching = client.post(
                "/same", headers={PAYMENT_SIGNATURE_HEADER: signature}
            )
            conflict = client.put(
                "/same", headers={PAYMENT_SIGNATURE_HEADER: signature}
            )
            release.set()
            first = first_future.result(timeout=5)

    assert first.status_code == 200
    assert first.json() == {"method": "post"}
    assert matching.status_code == 402
    assert conflict.status_code == 409
    assert conflict.json()["error"].startswith("payment-identifier was reused")
    assert handled == {"post": 1, "put": 0}
    assert events.count("verify") == 3
    assert facilitator.settle_count == 1


def test_verify_handler_settle_order_and_canonical_headers() -> None:
    events: list[str] = []
    facilitator = FakeFacilitator(events=events)
    app = FastAPI()
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"GET /paid": route()},
        facilitator_client=facilitator,
    )

    @app.get("/paid")
    async def paid(request: Request):
        events.append("handler")
        assert not hasattr(request.state, "x402_payment")
        return StreamingResponse(iter([b'{"secret":true}']), media_type="application/json")

    with TestClient(app) as client:
        signature, challenge = payment_header(client, "/paid")
        response = client.get(
            "/paid", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert response.status_code == 200
    assert response.json() == {"secret": True}
    assert events == ["verify", "handler", "settle"]
    assert challenge.resource.service_name == "Merchant"
    settlement = decode_payment_response_header(
        response.headers[PAYMENT_RESPONSE_HEADER]
    )
    assert settlement.success is True
    assert "private" in response.headers["cache-control"]


def test_invalid_payment_and_handler_failure_do_not_settle() -> None:
    invalid = FakeFacilitator(valid=False)
    invalid_app = FastAPI()
    calls = {"handler": 0}
    invalid_app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"GET /paid": route()},
        facilitator_client=invalid,
    )

    @invalid_app.get("/paid")
    async def invalid_handler():
        calls["handler"] += 1
        return {"secret": True}

    with TestClient(invalid_app) as client:
        signature, _ = payment_header(client, "/paid")
        response = client.get(
            "/paid", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )
    assert response.status_code == 402
    assert calls["handler"] == 0
    assert invalid.settle_count == 0

    failing = FakeFacilitator()
    failing_app = FastAPI()
    failing_app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"GET /paid": route()},
        facilitator_client=failing,
    )

    @failing_app.get("/paid")
    async def failing_handler():
        return JSONResponse({"error": "no"}, status_code=500)

    with TestClient(failing_app) as client:
        signature, _ = payment_header(client, "/paid")
        response = client.get(
            "/paid", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )
    assert response.status_code == 500
    assert failing.settle_count == 0


def test_wildcard_route_settles_successful_redirect() -> None:
    facilitator = FakeFacilitator()
    app = FastAPI()
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"GET /items/*": route()},
        facilitator_client=facilitator,
    )

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return RedirectResponse(f"/delivered/{item_id}", status_code=302)

    with TestClient(app, follow_redirects=False) as client:
        signature, _ = payment_header(client, "/items/42")
        response = client.get(
            "/items/42", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/delivered/42"
    assert facilitator.settle_count == 1
    assert PAYMENT_RESPONSE_HEADER in response.headers


def test_pending_settlement_reconciles_before_releasing_original_response() -> None:
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        error_message="awaiting validation",
        payer="rPayer",
        transaction="9" * 64,
        network="xrpl:1",
        amount="1000",
    )
    success = SettleResponse(
        success=True,
        payer="rPayer",
        transaction="9" * 64,
        network="xrpl:1",
        amount="1000",
    )
    events: list[str] = []
    facilitator = FakeFacilitator(
        settlements=[pending, success],
        events=events,
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    app = FastAPI()
    handled = {"count": 0}
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"POST /unsafe": route(method="POST")},
        facilitator_client=facilitator,
        response_store=store,
    )

    @app.post("/unsafe", status_code=201)
    async def unsafe() -> dict[str, int]:
        handled["count"] += 1
        return {"created": handled["count"]}

    with TestClient(app) as client:
        signature, _ = payment_header(client, "/unsafe")
        response = client.post(
            "/unsafe", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert response.status_code == 201
    assert response.json() == {"created": 1}
    assert handled["count"] == 1
    assert facilitator.verify_count == 1
    assert facilitator.settle_count == 2
    assert events == ["verify", "settle", "settle"]
    receipt = decode_payment_response_header(
        response.headers[PAYMENT_RESPONSE_HEADER]
    )
    assert receipt.success is True
    assert receipt.transaction == "9" * 64


def test_pending_unsafe_retry_does_not_repeat_handler() -> None:
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        error_message="awaiting validation",
        payer="rPayer",
        transaction="B" * 64,
        network="xrpl:1",
        amount="1000",
    )
    success = SettleResponse(
        success=True,
        payer="rPayer",
        transaction="B" * 64,
        network="xrpl:1",
        amount="1000",
    )
    facilitator = FakeFacilitator(
        verifications=[
            VerifyResponse(is_valid=True, payer="rPayer"),
            VerifyResponse(
                is_valid=False,
                invalid_reason=(
                    "invalid_exact_xrpl_payload_sequence_not_current"
                ),
                invalid_message="Account Sequence advanced after submission",
                payer="rPayer",
            ),
        ],
        settlements=[pending, pending, success],
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    app = FastAPI()
    handled = {"count": 0}
    seen_requests: list[Request] = []
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"POST /unsafe": route(method="POST")},
        facilitator_client=facilitator,
        response_store=store,
    )

    @app.post("/unsafe", status_code=201)
    async def unsafe(request: Request):
        handled["count"] += 1
        seen_requests.append(request)
        return {"created": handled["count"]}

    with TestClient(app) as client:
        signature, challenge = payment_header(client, "/unsafe")
        declaration = challenge.extensions["payment-identifier"]
        assert declaration["info"]["required"] is True
        first = client.post(
            "/unsafe", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )
        second = client.post(
            "/unsafe", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert first.status_code == 402
    assert first.content in {b"{}", b"null"}
    assert second.status_code == 201
    assert second.json() == {"created": 1}
    assert handled["count"] == 1
    assert facilitator.verify_count == 2
    assert facilitator.settle_count == 3
    context = seen_requests[0].state.x402_payment
    assert isinstance(context, XRPLPaymentContext)
    assert context.settlement.error_reason == "settlement_pending"
    assert context.accepted.amount == "1000"


def test_first_use_stale_authorization_failure_is_not_recovered() -> None:
    facilitator = FakeFacilitator(
        verifications=[
            VerifyResponse(
                is_valid=False,
                invalid_reason=(
                    "invalid_exact_xrpl_payload_sequence_not_current"
                ),
                invalid_message="Account Sequence is stale",
                payer="rPayer",
            )
        ]
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    app = FastAPI()
    handled = {"count": 0}
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"POST /unsafe": route(method="POST")},
        facilitator_client=facilitator,
        response_store=store,
    )

    @app.post("/unsafe")
    async def unsafe() -> dict[str, bool]:
        handled["count"] += 1
        return {"unexpected": True}

    with TestClient(app) as client:
        signature, _ = payment_header(client, "/unsafe")
        response = client.post(
            "/unsafe", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert response.status_code == 402
    assert handled["count"] == 0
    assert facilitator.verify_count == 1
    assert facilitator.settle_count == 0


def test_pending_unsafe_binary_retry_restores_exact_response() -> None:
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        error_message="awaiting validation",
        payer="rPayer",
        transaction="D" * 64,
        network="xrpl:1",
        amount="1000",
    )
    success = SettleResponse(
        success=True,
        payer="rPayer",
        transaction="D" * 64,
        network="xrpl:1",
        amount="1000",
    )
    facilitator = FakeFacilitator(
        settlements=[pending, pending, success]
    )
    store = RedisResourceResponseStore(AsyncStringRedis())
    app = FastAPI()
    handled = {"count": 0}
    binary_body = b"\x00\xff\x80exact\r\nbytes"
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"POST /unsafe": route(method="POST")},
        facilitator_client=facilitator,
        response_store=store,
    )

    @app.post("/unsafe")
    async def unsafe_binary() -> StreamingResponse:
        handled["count"] += 1
        return StreamingResponse(
            iter([binary_body[:4], binary_body[4:]]),
            status_code=206,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="result.bin"',
                "X-Resource-Version": "7",
            },
        )

    with TestClient(app) as client:
        signature, _ = payment_header(client, "/unsafe")
        first = client.post(
            "/unsafe", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )
        second = client.post(
            "/unsafe", headers={PAYMENT_SIGNATURE_HEADER: signature}
        )

    assert first.status_code == 402
    assert binary_body not in first.content
    assert second.status_code == 206
    assert second.content == binary_body
    assert second.headers["content-type"] == "application/octet-stream"
    assert second.headers["content-disposition"] == 'attachment; filename="result.bin"'
    assert second.headers["x-resource-version"] == "7"
    assert PAYMENT_RESPONSE_HEADER in second.headers
    assert handled["count"] == 1
    assert facilitator.settle_count == 3


def test_upstream_mcp_client_receives_pending_settlement_from_raw_meta() -> None:
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        error_message="awaiting validation",
        payer="rPayer",
        transaction="E" * 64,
        network="xrpl:1",
        amount="1000",
    )
    facilitator = FakeFacilitator(settlements=[pending])
    accepted = PaymentRequirements(
        scheme="exact",
        network="xrpl:1",
        asset="XRP",
        amount="1000",
        pay_to=DESTINATION,
        max_timeout_seconds=60,
        extra={
            "areFeesSponsored": False,
            "assetTransferMethod": "sequence",
        },
    )
    wrapper = create_xrpl_mcp_payment_wrapper(
        facilitator,
        accepts=[accepted],
        resource=ResourceInfo(
            url="mcp://merchant/write_report",
            description="Write report",
            mime_type="application/json",
        ),
        non_idempotent=True,
        enable_bazaar=False,
        response_store=RedisResourceResponseStore(AsyncStringRedis()),
    )

    @wrapper
    async def write_report(topic: str) -> dict[str, str]:
        return {"topic": topic}

    extensions = {
        "payment-identifier": declare_payment_identifier_extension(required=True)
    }
    append_payment_identifier_to_extensions(
        extensions, "pay_mcp_client_pending_0001"
    )
    payload = PaymentPayload(
        payload={"signedTxBlob": "AA"},
        accepted=accepted,
        resource=ResourceInfo(
            url="mcp://merchant/write_report",
            description="Write report",
            mime_type="application/json",
        ),
        extensions=extensions,
    )

    class DirectMCPClient:
        def __init__(self) -> None:
            self.raw_result: Any = None

        async def call_tool(self, params: dict[str, Any], **_kwargs: Any) -> Any:
            context = SimpleNamespace(
                request_context=SimpleNamespace(
                    meta=SimpleNamespace(model_extra=params.get("_meta", {}))
                )
            )
            self.raw_result = await write_report(
                topic=params["arguments"]["topic"], ctx=context
            )
            return self.raw_result

    direct_client = DirectMCPClient()
    upstream = x402MCPClient(direct_client, SimpleNamespace())
    result = asyncio.run(
        upstream.call_tool_with_payment(
            "write_report", {"topic": "XRPL"}, payload
        )
    )

    expected_pending = pending.model_dump(by_alias=True, exclude_none=True)
    assert direct_client.raw_result.model_dump(by_alias=True)["_meta"][
        MCP_PAYMENT_RESPONSE_META_KEY
    ] == expected_pending
    assert getattr(direct_client.raw_result, "_meta")[
        MCP_PAYMENT_RESPONSE_META_KEY
    ] == expected_pending
    assert result.payment_response == pending
    assert result.is_error is True
    assert facilitator.settle_count == 2


def test_unsafe_payment_without_required_identifier_never_runs_handler() -> None:
    events: list[str] = []
    facilitator = FakeFacilitator(events=events)
    store = RedisResourceResponseStore(AsyncStringRedis())
    app = FastAPI()
    handled = {"count": 0}
    app.add_middleware(
        PaymentMiddlewareASGI,
        routes={"POST /unsafe": route(method="POST")},
        facilitator_client=facilitator,
        response_store=store,
    )

    @app.post("/unsafe")
    async def unsafe_without_identifier() -> dict[str, bool]:
        handled["count"] += 1
        return {"created": True}

    with TestClient(app) as client:
        challenge_response = client.post("/unsafe")
        challenge = decode_payment_required_header(
            challenge_response.headers[PAYMENT_REQUIRED_HEADER]
        )
        payload = PaymentPayload(
            payload={"signedTxBlob": "AA"},
            accepted=challenge.accepts[0],
            resource=challenge.resource,
            extensions=json.loads(json.dumps(challenge.extensions or {})),
        )
        response = client.post(
            "/unsafe",
            headers={
                PAYMENT_SIGNATURE_HEADER: encode_payment_signature_header(payload)
            },
        )

    assert response.status_code == 402
    assert PAYMENT_REQUIRED_HEADER in response.headers
    assert handled["count"] == 0
    assert facilitator.settle_count == 0
    assert events == ["verify"]


def test_unsafe_route_requires_response_store() -> None:
    app = FastAPI()
    with pytest.raises(
        ValueError,
        match="unsafe paid routes require a RedisResourceResponseStore",
    ):
        PaymentMiddlewareASGI(
            app,
            routes={"POST /unsafe": route(method="POST")},
            facilitator_client=FakeFacilitator(),
        )

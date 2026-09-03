from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from redis.asyncio import from_url
from x402.extensions.bazaar import (
    DeclareMcpDiscoveryConfig,
    declare_mcp_discovery_extension,
)
from x402.schemas import PaymentRequirements

from xrpl_x402_middleware import (
    RedisResourceResponseStore,
    build_facilitator_client,
    create_xrpl_mcp_payment_wrapper,
    ResourceInfo,
)


def create_mcp_server() -> FastMCP:
    facilitator = build_facilitator_client(
        base_url=os.getenv("FACILITATOR_URL", "http://127.0.0.1:8000"),
        bearer_token=os.environ["FACILITATOR_TOKEN"],
    )
    response_store = RedisResourceResponseStore(
        from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    )
    accepts = [
        PaymentRequirements(
            scheme="exact",
            network=os.getenv("XRPL_NETWORK", "xrpl:1"),
            asset="XRP",
            amount="1000",
            pay_to=os.environ["MERCHANT_XRPL_ADDRESS"],
            max_timeout_seconds=60,
            extra={
                "areFeesSponsored": False,
                "assetTransferMethod": "sequence",
            },
        )
    ]
    discovery = declare_mcp_discovery_extension(
        DeclareMcpDiscoveryConfig(
            tool_name="generate_report",
            description="Generate a paid report",
            input_schema={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
                "additionalProperties": False,
            },
            example={"topic": "XRPL"},
        )
    )
    paid = create_xrpl_mcp_payment_wrapper(
        facilitator,
        accepts=accepts,
        resource=ResourceInfo(
            url="mcp://merchant/generate_report",
            description="Generate a paid report",
            mime_type="application/json",
            service_name="XRPL reports",
            tags=["xrpl", "report"],
        ),
        extensions=discovery,
        non_idempotent=True,
        response_store=response_store,
    )
    server = FastMCP("xrpl-paid-tools")

    @server.tool()
    @paid
    async def generate_report(topic: str) -> dict[str, str]:
        return {"topic": topic, "report": f"Validated paid report about {topic}"}

    return server


if __name__ == "__main__":
    create_mcp_server().run(transport="sse")

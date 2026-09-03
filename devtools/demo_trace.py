from __future__ import annotations

import asyncio
import json
import os

from xrpl_x402_payer import pay_with_x402


async def main() -> None:
    asset = os.getenv("PAYMENT_ASSET", "").strip() or None
    issuer = os.getenv("PAYMENT_ASSET_ISSUER", "").strip() or None
    max_spend = os.getenv("PAYMENT_MAX_SPEND", "").strip() or None
    result = await pay_with_x402(
        url=os.getenv("TARGET_URL", "http://127.0.0.1:8010/premium"),
        asset=asset,
        issuer=issuer,
        max_spend=max_spend,
    )
    summary = {
        "statusCode": result.status_code,
        "paid": result.paid,
        "pending": result.pending,
        "body": result.text,
        "accepted": (
            result.accepted.model_dump(by_alias=True, exclude_none=True)
            if result.accepted
            else None
        ),
        "settlement": (
            result.payment_response.model_dump(by_alias=True, exclude_none=True)
            if result.payment_response
            else None
        ),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())

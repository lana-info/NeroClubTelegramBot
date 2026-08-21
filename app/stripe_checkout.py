from __future__ import annotations

from typing import Any

import httpx


class StripeCheckoutError(RuntimeError):
    pass


async def create_checkout_session(
    secret_key: str,
    price_id: str,
    user_id: int,
    *,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    if not secret_key or not price_id or not success_url or not cancel_url:
        raise StripeCheckoutError("Stripe Checkout is not configured")
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": str(user_id),
        "metadata[internal_user_id]": str(user_id),
        "subscription_data[metadata][internal_user_id]": str(user_id),
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if customer_email:
        data["customer_email"] = customer_email
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        response = await client.post(
            "https://api.stripe.com/v1/checkout/sessions",
            data=data,
            auth=(secret_key, ""),
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise StripeCheckoutError("Stripe returned invalid JSON") from exc
    if response.status_code >= 400 or not body.get("url"):
        message = ((body.get("error") or {}).get("message") if isinstance(body, dict) else None)
        raise StripeCheckoutError(message or "Stripe Checkout session creation failed")
    return body

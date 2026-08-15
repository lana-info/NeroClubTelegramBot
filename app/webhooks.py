from __future__ import annotations

import hashlib
import hmac
import json
import time


def verify_stripe_signature(payload: bytes, signature: str, secret: str, tolerance: int) -> None:
    if not secret:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not configured")
    parts: dict[str, list[str]] = {}
    for item in signature.split(","):
        key, separator, value = item.partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    timestamp = parts.get("t", [""])[0]
    candidates = parts.get("v1", [])
    if not timestamp or not candidates:
        raise ValueError("invalid Stripe signature header")
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise ValueError("invalid Stripe timestamp") from exc
    if abs(time.time() - timestamp_int) > tolerance:
        raise ValueError("Stripe signature timestamp outside tolerance")
    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise ValueError("invalid Stripe signature")


def parse_event(payload: bytes) -> dict:
    event = json.loads(payload)
    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise ValueError("invalid webhook event")
    return event

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any


SUPPORTED_EVENTS = {
    "invoice.paid",
    "invoice.payment_failed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}


def _paid_until(obj: dict[str, Any]) -> str | None:
    value = obj.get("period_end") or obj.get("current_period_end")
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def apply_stripe_event(db: sqlite3.Connection, event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if event_type not in SUPPORTED_EVENTS:
        return "ignored"
    obj = ((event.get("data") or {}).get("object") or {})
    metadata = obj.get("metadata") or {}
    try:
        user_id = int(metadata["internal_user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Stripe event has no valid internal_user_id metadata") from exc
    if not db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        raise ValueError("Stripe event references an unknown user")

    subscription_id = obj.get("subscription") or obj.get("id")
    if not isinstance(subscription_id, str) or not subscription_id:
        raise ValueError("Stripe event has no subscription ID")
    customer_id = obj.get("customer")
    paid_until = _paid_until(obj)
    if event_type == "invoice.paid":
        billing_status, payment_status = "active", "paid"
    elif event_type == "invoice.payment_failed":
        billing_status, payment_status = "past_due", "failed"
    elif event_type == "customer.subscription.deleted":
        billing_status, payment_status = "cancelled", "cancelled"
    else:
        billing_status = str(obj.get("status") or "pending")
        payment_status = "paid" if billing_status in {"active", "trialing"} else billing_status

    db.execute(
        """INSERT INTO subscriptions
           (user_id, provider, provider_subscription_id, provider_customer_id,
            billing_status, payment_status, provider_paid_until)
           VALUES (?, 'stripe', ?, ?, ?, ?, ?)
           ON CONFLICT(provider, provider_subscription_id) DO UPDATE SET
             user_id = excluded.user_id,
             provider_customer_id = COALESCE(excluded.provider_customer_id, subscriptions.provider_customer_id),
             billing_status = excluded.billing_status,
             payment_status = excluded.payment_status,
             provider_paid_until = COALESCE(excluded.provider_paid_until, subscriptions.provider_paid_until),
             updated_at = CURRENT_TIMESTAMP""",
        (user_id, subscription_id, customer_id, billing_status, payment_status, paid_until),
    )
    db.execute(
        "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, ?, ?, ?)",
        ("stripe", f"stripe.{event_type}", user_id, json.dumps({"event_id": event.get("id")}, ensure_ascii=False)),
    )
    return "processed"


def process_pending_stripe_events(db: sqlite3.Connection, limit: int = 20) -> dict[str, int]:
    jobs = db.execute(
        "SELECT id, aggregate_key, payload FROM outbox_jobs WHERE kind = 'stripe.event' AND status = 'pending' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    processed = ignored = failed = 0
    for job in jobs:
        try:
            result = apply_stripe_event(db, json.loads(job["payload"]))
            db.execute("UPDATE outbox_jobs SET status = 'done', processed_at = CURRENT_TIMESTAMP WHERE id = ?", (job["id"],))
            db.execute("UPDATE inbox_events SET processed_at = CURRENT_TIMESTAMP WHERE provider = 'stripe' AND external_event_id = ?", (job["aggregate_key"],))
            processed += result == "processed"
            ignored += result == "ignored"
        except (ValueError, json.JSONDecodeError) as exc:
            db.execute(
                "UPDATE outbox_jobs SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc), job["id"]),
            )
            failed += 1
    return {"processed": processed, "ignored": ignored, "failed": failed}

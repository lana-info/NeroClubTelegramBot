from __future__ import annotations

import json
import hmac
import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .access import apply_command, effective_access, upsert_user
from .config import settings
from .db import Database
from .webhooks import parse_event, verify_stripe_signature


app = FastAPI(title="Nero Club Subscription Backend", version="0.1.0")
db = Database(settings.database_url)
db.init_schema()


def require_admin(authorization: str | None = Header(default=None)) -> str:
    if not settings.admin_api_token:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    expected = f"Bearer {settings.admin_api_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return "admin-api"


@app.get("/health")
def health() -> dict[str, Any]:
    with db.connect() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok", "dry_run": settings.dry_run}


@app.post("/internal/users", status_code=201)
def create_or_update_user(payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        with db.connect() as connection:
            user = upsert_user(connection, payload)
            return {"id": user["id"], "telegram_id": user["telegram_id"], "status": "stored"}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/internal/sheets/commands")
def sheet_command(payload: dict[str, Any], actor: str = Depends(require_admin)) -> dict[str, Any]:
    command_id = payload.get("command_id")
    user_id = payload.get("user_id")
    action = payload.get("action")
    if not isinstance(command_id, str) or not command_id or not isinstance(user_id, int) or not isinstance(action, str):
        raise HTTPException(status_code=422, detail="command_id, user_id and action are required")
    try:
        with db.connect() as connection:
            if not connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
                raise HTTPException(status_code=404, detail="user not found")
            return apply_command(connection, command_id, user_id, action, payload, actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(default=None)) -> dict[str, str]:
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Stripe-Signature is required")
    try:
        verify_stripe_signature(
            payload,
            stripe_signature,
            settings.stripe_webhook_secret,
            settings.stripe_signature_tolerance_seconds,
        )
        event = parse_event(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid webhook") from exc

    with db.connect() as connection:
        try:
            connection.execute(
                "INSERT INTO inbox_events(provider, external_event_id, event_type, payload) VALUES (?, ?, ?, ?)",
                ("stripe", event["id"], event["type"], payload.decode("utf-8")),
            )
        except sqlite3.IntegrityError:
            return {"status": "duplicate"}

        # Business processing is intentionally queued. External side effects are not
        # performed inside the webhook request and can be retried by a worker.
        connection.execute(
            "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
            ("stripe.event", event["id"], payload.decode("utf-8")),
        )
    return {"status": "accepted"}


@app.get("/internal/users/{user_id}/access")
def user_access(user_id: int, _: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        subscription = connection.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
        return {"user_id": user_id, "effective_access": effective_access(user, subscription)}

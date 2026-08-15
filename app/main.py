from __future__ import annotations

import json
import hmac
import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from .access import apply_command, effective_access, upsert_user
from .config import settings
from .db import Database
from .webhooks import parse_event, verify_stripe_signature
from .integrations.telegram import TelegramClient, TelegramError
from .integrations.wordpress import WordPressClient
from .telegram_updates import process_update
from .stripe_events import process_pending_stripe_events
from .dashboard import rows_as_csv, rows_for_dashboard


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


@app.post("/webhooks/telegram")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, str]:
    if not settings.telegram_bot_token or not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured")
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", settings.telegram_webhook_secret):
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        update = await request.json()
        telegram = TelegramClient(settings.telegram_bot_token)
        wordpress = None
        if settings.wordpress_base_url and settings.wordpress_shared_secret:
            wordpress = WordPressClient(settings.wordpress_base_url, settings.wordpress_shared_secret)
        with db.connect() as connection:
            result = await process_update(
                connection, update, telegram, chat_id=settings.telegram_chat_id,
                wordpress=wordpress, temporary_password_hours=settings.temporary_password_hours,
            )
        return {"status": result}
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid Telegram update") from exc
    except TelegramError as exc:
        raise HTTPException(status_code=502, detail="Telegram API error") from exc


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


@app.post("/internal/jobs/process-stripe")
def process_stripe_jobs(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        return process_pending_stripe_events(connection)


@app.get("/internal/sheets/preview")
def sheets_preview(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        rows = rows_for_dashboard(connection)
    return {"headers": rows[0], "rows": rows[1:], "count": len(rows) - 1}


@app.get("/internal/sheets/export.csv", response_class=PlainTextResponse)
def sheets_export(_: str = Depends(require_admin)) -> str:
    with db.connect() as connection:
        return rows_as_csv(rows_for_dashboard(connection))

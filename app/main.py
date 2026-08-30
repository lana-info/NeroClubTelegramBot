from __future__ import annotations

import json
import hmac
import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from .access import apply_command, effective_access, upsert_user
from .keys import AppKeyError, create_app_key, revoke_app_key, sync_app_key_rows
from .config import settings
from .db import Database
from .webhooks import parse_event, verify_stripe_signature
from .integrations.telegram import TelegramClient, TelegramError
from .integrations.wordpress import WordPressClient
from .telegram_updates import process_update
from .telegram_menu import BOT_COMMANDS
from .stripe_events import process_pending_stripe_events
from .site_access import process_pending_site_access_jobs
from .membership import (
    MembershipError,
    create_personal_invite,
    process_pending_telegram_invite_jobs,
    process_pending_telegram_restore_jobs,
    reconcile_members,
    revoke_expired_telegram_invites,
)
from .reminders import send_subscription_reminders
from .dashboard import rows_as_csv, rows_for_dashboard
from .sheets import dashboard_rows, import_users, rows_for_site_access_sheet, rows_for_users_sheet


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


@app.post("/internal/sheets/import")
def import_sheet_snapshot(payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    rows = payload.get("users")
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="users must be a list")
    try:
        with db.connect() as connection:
            return {"users": import_users(connection, rows)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/internal/sheets/users")
def sheets_users(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        rows = rows_for_users_sheet(connection)
    return {"headers": rows[0], "rows": rows[1:], "count": len(rows) - 1}


@app.get("/internal/sheets/site-access")
def sheets_site_access(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        rows = rows_for_site_access_sheet(connection)
    return {"headers": rows[0], "rows": rows[1:], "count": len(rows) - 1}


@app.get("/internal/sheets/dashboard")
def sheets_dashboard(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        rows = dashboard_rows(connection)
    return {"headers": rows[0], "rows": rows[1:], "count": len(rows) - 1}


@app.post("/internal/telegram/setup-menu")
async def setup_telegram_menu(_: str = Depends(require_admin)) -> dict[str, str]:
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN is not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    await telegram.set_my_commands(BOT_COMMANDS)
    if settings.telegram_webhook_url:
        if not settings.telegram_webhook_secret:
            raise HTTPException(status_code=503, detail="TELEGRAM_WEBHOOK_SECRET is not configured")
        await telegram.set_webhook(settings.telegram_webhook_url, settings.telegram_webhook_secret)
    return {"status": "configured", "webhook": "configured" if settings.telegram_webhook_url else "unchanged"}


@app.post("/internal/app-keys", status_code=201)
def create_or_update_app_key(payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    try:
        with db.connect() as connection:
            key = create_app_key(connection, payload, settings.app_keys_encryption_key)
            return {
                "key_id": key["external_key_id"], "app_name": key["app_name"],
                "assigned_user_id": key["assigned_user_id"], "status": key["status"],
                "key_expires_at": key["key_expires_at"],
            }
    except (AppKeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/internal/app-keys/{key_id}/revoke")
def revoke_key(key_id: str, _: str = Depends(require_admin)) -> dict[str, str]:
    with db.connect() as connection:
        revoke_app_key(connection, key_id)
    return {"key_id": key_id, "status": "revoked"}


@app.post("/internal/app-keys/sync")
def sync_keys(payload: dict[str, Any], _: str = Depends(require_admin)) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > 500:
        raise HTTPException(status_code=422, detail="rows must be a list with at most 500 items")
    try:
        with db.connect() as connection:
            return sync_app_key_rows(connection, rows, settings.app_keys_encryption_key)
    except AppKeyError as exc:
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
    if settings.payment_source == "sheet":
        return {"status": "disabled", "reason": "payment source is Google Sheets"}
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
            telegram_chat_ids = tuple(
                chat_id
                for chat_id in (settings.telegram_chat_id, settings.telegram_channel_id)
                if chat_id
            )
            result = await process_update(
                connection, update, telegram, chat_id=telegram_chat_ids,
                wordpress=wordpress,
                app_keys_encryption_key=settings.app_keys_encryption_key,
                admin_telegram_ids=settings.admin_telegram_ids,
                payment_url=settings.payment_url,
                payment_source=settings.payment_source,
                stripe_secret_key=settings.stripe_secret_key,
                stripe_price_id=settings.stripe_price_id,
                checkout_success_url=settings.checkout_success_url,
                checkout_cancel_url=settings.checkout_cancel_url,
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


@app.post("/internal/users/{user_id}/invite")
async def user_invite(user_id: int, _: str = Depends(require_admin)) -> dict[str, Any]:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=503, detail="Telegram chat integration is not configured")
    try:
        telegram = TelegramClient(settings.telegram_bot_token)
        with db.connect() as connection:
            return await create_personal_invite(
                connection, user_id, telegram, settings.telegram_chat_id, dry_run=settings.dry_run
            )
    except MembershipError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/internal/jobs/reconcile-telegram")
async def reconcile_telegram(_: str = Depends(require_admin)) -> dict[str, int]:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=503, detail="Telegram chat integration is not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    with db.connect() as connection:
        chat_ids = [settings.telegram_chat_id]
        if settings.telegram_channel_id and settings.telegram_channel_id not in chat_ids:
            chat_ids.append(settings.telegram_channel_id)
        totals = {"checked": 0, "active": 0, "denied": 0, "removed": 0, "would_remove": 0, "failed": 0}
        for chat_id in chat_ids:
            result = await reconcile_members(
                connection, telegram, chat_id, dry_run=settings.dry_run
            )
            for key in totals:
                totals[key] += result[key]
        return totals


@app.post("/internal/jobs/process-telegram-restores")
async def process_telegram_restores(_: str = Depends(require_admin)) -> dict[str, int]:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=503, detail="Telegram chat integration is not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    with db.connect() as connection:
        return await process_pending_telegram_restore_jobs(
            connection,
            telegram,
            settings.telegram_chat_id,
            additional_chat_ids=((settings.telegram_channel_id,) if settings.telegram_channel_id else ()),
            dry_run=settings.dry_run,
        )


@app.post("/internal/jobs/process-telegram-invites")
async def process_telegram_invites(_: str = Depends(require_admin)) -> dict[str, int]:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise HTTPException(status_code=503, detail="Telegram chat integration is not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    chat_ids = tuple(
        chat_id for chat_id in (settings.telegram_chat_id, settings.telegram_channel_id) if chat_id
    )
    with db.connect() as connection:
        return await process_pending_telegram_invite_jobs(
            connection, telegram, chat_ids, dry_run=settings.dry_run
        )


@app.post("/internal/jobs/revoke-telegram-invites")
async def revoke_telegram_invites(_: str = Depends(require_admin)) -> dict[str, int]:
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN is not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    with db.connect() as connection:
        return await revoke_expired_telegram_invites(
            connection, telegram, dry_run=settings.dry_run
        )


@app.post("/internal/jobs/send-reminders")
async def send_reminders(_: str = Depends(require_admin)) -> dict[str, int]:
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN is not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    with db.connect() as connection:
        return await send_subscription_reminders(
            connection,
            telegram,
            payment_url=settings.payment_url if settings.payment_source != "sheet" else "",
            dry_run=settings.dry_run,
        )


@app.post("/internal/jobs/process-stripe")
def process_stripe_jobs(_: str = Depends(require_admin)) -> dict[str, Any]:
    if settings.payment_source == "sheet":
        return {"processed": 0, "failed": 0, "skipped": 0, "status": "disabled"}
    with db.connect() as connection:
        return process_pending_stripe_events(connection)


@app.post("/internal/jobs/process-site-access")
async def process_site_access_jobs(_: str = Depends(require_admin)) -> dict[str, Any]:
    if not settings.telegram_bot_token or not settings.wordpress_base_url or not settings.wordpress_shared_secret:
        raise HTTPException(status_code=503, detail="Telegram and WordPress integrations are not configured")
    telegram = TelegramClient(settings.telegram_bot_token)
    wordpress = WordPressClient(settings.wordpress_base_url, settings.wordpress_shared_secret)
    with db.connect() as connection:
        return await process_pending_site_access_jobs(connection, telegram, wordpress, dry_run=settings.dry_run)


@app.get("/internal/sheets/preview")
def sheets_preview(_: str = Depends(require_admin)) -> dict[str, Any]:
    with db.connect() as connection:
        rows = rows_for_dashboard(connection)
    return {"headers": rows[0], "rows": rows[1:], "count": len(rows) - 1}


@app.get("/internal/sheets/export.csv", response_class=PlainTextResponse)
def sheets_export(_: str = Depends(require_admin)) -> str:
    with db.connect() as connection:
        return rows_as_csv(rows_for_dashboard(connection))

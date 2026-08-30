from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from .access import effective_access
from .integrations.telegram import TelegramClient, TelegramError
from .integrations.wordpress import WordPressClient, WordPressError


class SiteAccessError(RuntimeError):
    pass


def queue_site_access_job(
    db: sqlite3.Connection, user_id: int, action: str, aggregate_key: str
) -> None:
    if action not in {"deactivate", "restore"}:
        raise ValueError("unsupported site access action")
    db.execute(
        "INSERT OR IGNORE INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
        (f"site.{action}", aggregate_key, json.dumps({"user_id": user_id, "action": action})),
    )


async def issue_site_credentials(
    db: sqlite3.Connection,
    user_id: int,
    telegram: TelegramClient,
    wordpress: WordPressClient,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        raise SiteAccessError("user not found")
    subscription = db.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()
    if effective_access(user, subscription) != "active":
        raise SiteAccessError("site access requires an active subscription")
    if not isinstance(user["telegram_id"], int):
        raise SiteAccessError("telegram_id is required")

    password = secrets.token_urlsafe(18)
    try:
        result = await wordpress.sync_user(
            {
                "action": "create_or_activate",
                "user_id": user["wordpress_user_id"] or 0,
                "username": user["wordpress_login"] or "",
                "email": user["wordpress_email"] or "",
                "role": user["wordpress_role"] or "subscriber",
                "password": password,
            },
            idempotency_key,
        )
        await telegram.send_message(
            user["telegram_id"],
            "Доступ к сайту выдан.\n"
            f"Логин: {result['login']}\n"
            f"Постоянный пароль: {password}\n\n"
            "Не пересылайте это сообщение. При повторном запросе пароль будет заменён новым.",
        )
    except (WordPressError, TelegramError, KeyError) as exc:
        raise SiteAccessError("site access delivery failed") from exc
    return {"user_id": user_id, "login": result["login"], "password_delivered": True}


async def process_pending_site_access_jobs(
    db: sqlite3.Connection,
    telegram: TelegramClient,
    wordpress: WordPressClient,
    *,
    limit: int = 20,
    dry_run: bool = False,
    wordpress_access_enabled: bool = True,
    wordpress_deactivation_enabled: bool = True,
) -> dict[str, int]:
    jobs = db.execute(
        "SELECT id, kind, aggregate_key, payload, attempts FROM outbox_jobs "
        "WHERE kind IN ('site.credentials', 'site.deactivate', 'site.restore') "
        "AND status = 'pending' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    if dry_run:
        return {"processed": 0, "failed": 0, "skipped": len(jobs)}
    processed = failed = 0
    for job in jobs:
        payload = json.loads(job["payload"])
        try:
            if job["kind"] == "site.credentials" and not wordpress_access_enabled:
                continue
            if job["kind"] == "site.deactivate" and not wordpress_deactivation_enabled:
                continue
            if job["kind"] == "site.restore" and not wordpress_access_enabled:
                continue
            if job["kind"] == "site.credentials":
                result = await issue_site_credentials(
                    db,
                    int(payload["user_id"]),
                    telegram,
                    wordpress,
                    idempotency_key=f"{job['aggregate_key']}-attempt-{job['attempts'] + 1}",
                )
            else:
                user = db.execute("SELECT * FROM users WHERE id = ?", (int(payload["user_id"]),)).fetchone()
                if not user:
                    raise SiteAccessError("user not found")
                result = await wordpress.sync_user(
                    {
                        "action": payload["action"],
                        "user_id": user["wordpress_user_id"] or 0,
                        "username": user["wordpress_login"] or "",
                        "email": user["wordpress_email"] or "",
                    },
                    f"{job['aggregate_key']}-attempt-{job['attempts'] + 1}",
                )
            db.execute("UPDATE outbox_jobs SET status = 'done', processed_at = CURRENT_TIMESTAMP WHERE id = ?", (job["id"],))
            if payload.get("command_id"):
                db.execute(
                    "UPDATE sheets_commands SET status = 'done', result = ?, completed_at = CURRENT_TIMESTAMP WHERE command_id = ?",
                    (json.dumps(result, ensure_ascii=False), payload["command_id"]),
                )
            processed += 1
        except (SiteAccessError, WordPressError, ValueError, KeyError, json.JSONDecodeError) as exc:
            db.execute(
                "UPDATE outbox_jobs SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
                (str(exc), job["id"]),
            )
            if payload.get("command_id"):
                db.execute(
                    "UPDATE sheets_commands SET status = 'error', result = ?, completed_at = CURRENT_TIMESTAMP WHERE command_id = ?",
                    (json.dumps({"error": str(exc)}, ensure_ascii=False), payload["command_id"]),
                )
            failed += 1
    return {"processed": processed, "failed": failed}

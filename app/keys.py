from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .access import effective_access


class AppKeyError(RuntimeError):
    pass


def _fernet(encryption_key: str) -> Fernet:
    if not encryption_key:
        raise AppKeyError("APP_KEYS_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise AppKeyError("APP_KEYS_ENCRYPTION_KEY is invalid") from exc


def encrypt_key(secret: str, encryption_key: str) -> str:
    if not secret:
        raise ValueError("key is required")
    return _fernet(encryption_key).encrypt(secret.encode()).decode()


def decrypt_key(encrypted_key: str, encryption_key: str) -> str:
    try:
        return _fernet(encryption_key).decrypt(encrypted_key.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise AppKeyError("stored app key cannot be decrypted") from exc


def _is_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppKeyError("app key expiry has invalid format") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def create_app_key(
    db: sqlite3.Connection,
    payload: dict[str, Any],
    encryption_key: str,
) -> sqlite3.Row:
    external_key_id = payload.get("key_id")
    app_name = payload.get("app_name")
    user_id = payload.get("user_id")
    if not isinstance(external_key_id, str) or not external_key_id.strip():
        raise ValueError("key_id is required")
    if not isinstance(app_name, str) or not app_name.strip():
        raise ValueError("app_name is required")
    if not isinstance(payload.get("key"), str) or not payload["key"]:
        raise ValueError("key is required")
    if user_id is not None and not db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        raise ValueError("assigned user not found")
    expires_at = payload.get("key_expires_at")
    if expires_at:
        datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    status = payload.get("status") or "issued"
    if status not in {"available", "issued", "expired", "revoked"}:
        raise ValueError("invalid app key status")
    encrypted = encrypt_key(payload["key"], encryption_key)
    db.execute(
        """INSERT INTO app_keys
           (external_key_id, app_name, access_plan, encrypted_key, key_expires_at, assigned_user_id, status, issued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'issued' THEN CURRENT_TIMESTAMP ELSE NULL END)
           ON CONFLICT(external_key_id) DO UPDATE SET
             app_name = excluded.app_name,
             access_plan = excluded.access_plan,
             encrypted_key = excluded.encrypted_key,
             key_expires_at = excluded.key_expires_at,
             assigned_user_id = excluded.assigned_user_id,
             status = excluded.status,
             revoked_at = NULL,
             updated_at = CURRENT_TIMESTAMP""",
        (external_key_id, app_name.strip(), payload.get("access_plan"), encrypted, expires_at, user_id, status, status),
    )
    return db.execute("SELECT * FROM app_keys WHERE external_key_id = ?", (external_key_id,)).fetchone()


def sync_app_key_rows(
    db: sqlite3.Connection,
    rows: list[dict[str, Any]],
    encryption_key: str,
) -> dict[str, Any]:
    synced = revoked = 0
    errors: list[dict[str, Any]] = []
    for row_number, payload in enumerate(rows, start=2):
        try:
            action = str(payload.get("action") or "none").lower()
            status = str(payload.get("status") or "issued").lower()
            key_id = payload.get("key_id")
            if action == "none":
                continue
            if action == "revoke" or status in {"revoked", "expired"}:
                if not isinstance(key_id, str) or not key_id.strip():
                    raise ValueError("key_id is required")
                revoke_app_key(db, key_id)
                revoked += 1
                continue
            if action != "issue":
                raise ValueError("unsupported action")
            create_app_key(db, payload, encryption_key)
            synced += 1
        except (AppKeyError, ValueError, TypeError) as exc:
            errors.append({"row": row_number, "key_id": payload.get("key_id"), "error": str(exc)})
    return {"synced": synced, "revoked": revoked, "errors": errors}


def revoke_app_key(db: sqlite3.Connection, external_key_id: str) -> None:
    db.execute(
        "UPDATE app_keys SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE external_key_id = ?",
        (external_key_id,),
    )


def keys_for_user(db: sqlite3.Connection, user_id: int, encryption_key: str) -> list[dict[str, Any]]:
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    subscription = db.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()
    if not user or effective_access(user, subscription) != "active":
        return []
    rows = db.execute(
        "SELECT * FROM app_keys WHERE assigned_user_id = ? AND status = 'issued' ORDER BY app_name, id",
        (user_id,),
    ).fetchall()
    result = []
    for row in rows:
        if _is_expired(row["key_expires_at"]):
            db.execute(
                "UPDATE app_keys SET status = 'expired', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
            continue
        result.append({
            "app_name": row["app_name"],
            "key": decrypt_key(row["encrypted_key"], encryption_key),
            "key_expires_at": row["key_expires_at"],
        })
    return result


def display_expiry(value: str | None) -> str:
    if not value:
        return "бессрочный"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError as exc:
        raise AppKeyError("app key expiry has invalid format") from exc

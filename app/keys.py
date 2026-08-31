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
            normalized = dict(payload)
            normalized["user_id"] = _resolve_assigned_user_id(db, payload.get("assigned_user_id"))
            create_app_key(db, normalized, encryption_key)
            synced += 1
        except (AppKeyError, ValueError, TypeError) as exc:
            errors.append({"row": row_number, "key_id": payload.get("key_id"), "error": str(exc)})
    return {"synced": synced, "revoked": revoked, "errors": errors}


def _resolve_assigned_user_id(db: sqlite3.Connection, value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        numeric_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("assigned_user_id must be a number") from exc
    by_internal_id = db.execute("SELECT id FROM users WHERE id = ?", (numeric_value,)).fetchone()
    if by_internal_id:
        return int(by_internal_id["id"])
    by_telegram_id = db.execute("SELECT id FROM users WHERE telegram_id = ?", (numeric_value,)).fetchone()
    if by_telegram_id:
        return int(by_telegram_id["id"])
    raise ValueError("assigned user not found")


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
    if not user:
        return []
    club_access = effective_access(user, subscription) == "active"
    licensed_access = bool(db.execute(
        "SELECT 1 FROM app_keys WHERE assigned_user_id = ? AND access_plan = 'license' "
        "AND status = 'issued' LIMIT 1", (user_id,)
    ).fetchone())
    if not club_access and not licensed_access:
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


def sync_license_rows(
    db: sqlite3.Connection,
    rows: list[dict[str, Any]],
    encryption_key: str,
) -> dict[str, Any]:
    """Assign manually generated license-server keys to Telegram users.

    License rows are explicit grants and therefore intentionally bypass the
    club-subscription check used by the regular app-key tab.
    """
    synced = revoked = 0
    errors: list[dict[str, Any]] = []
    for row_number, payload in enumerate(rows, start=2):
        try:
            action = str(payload.get("action") or "none").lower()
            license_id = str(payload.get("license_id") or "").strip()
            if action == "none":
                continue
            if not license_id:
                raise ValueError("license_id is required")
            key_id = f"license-{license_id}"
            if action == "revoke" or str(payload.get("status") or "").lower() == "revoked":
                revoke_app_key(db, key_id)
                revoked += 1
                continue
            if action != "issue":
                raise ValueError("unsupported action")
            license_key = payload.get("license_key")
            product_id = str(payload.get("product_id") or "").strip()
            if not isinstance(license_key, str) or not license_key.strip():
                raise ValueError("license_key is required")
            if not product_id:
                raise ValueError("product_id is required")
            license_term = str(payload.get("license_term") or "perpetual").lower()
            if license_term not in {"perpetual", "custom"}:
                raise ValueError("license_term must be perpetual or custom")
            expires_at = payload.get("expires_at") or None
            if license_term == "custom" and not expires_at:
                raise ValueError("expires_at is required for a custom license")
            if license_term == "perpetual":
                expires_at = None
            telegram_id = payload.get("telegram_id")
            if telegram_id in (None, ""):
                raise ValueError("telegram_id is required")
            try:
                telegram_id = int(telegram_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("telegram_id must be a number") from exc
            assigned_user = db.execute(
                "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            if assigned_user:
                assigned_user_id = int(assigned_user["id"])
            else:
                from .access import upsert_user
                assigned_user_id = int(upsert_user(db, {
                    "telegram_id": telegram_id,
                    "telegram_username": payload.get("username"),
                    "wordpress_email": payload.get("email"),
                })["id"])
            create_app_key(db, {
                "key_id": key_id,
                "app_name": payload.get("app_name") or product_id,
                "access_plan": "license",
                "key": license_key,
                "key_expires_at": expires_at,
                "user_id": assigned_user_id,
                "status": "issued",
            }, encryption_key)
            synced += 1
        except (AppKeyError, ValueError, TypeError) as exc:
            errors.append({"row": row_number, "license_id": payload.get("license_id"), "error": str(exc)})
    return {"synced": synced, "revoked": revoked, "errors": errors}


def display_expiry(value: str | None) -> str:
    if not value:
        return "бессрочный"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError as exc:
        raise AppKeyError("app key expiry has invalid format") from exc

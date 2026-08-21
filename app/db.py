from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


def _path_from_url(url: str) -> str:
    if not url.startswith("sqlite:///"):
        raise ValueError("This MVP currently supports only sqlite:/// DATABASE_URL")
    path = Path(url.removeprefix("sqlite:///"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


class Database:
    def __init__(self, url: str):
        self.path = _path_from_url(url)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    telegram_username TEXT,
                    wordpress_user_id INTEGER,
                    wordpress_email TEXT,
                    wordpress_role TEXT,
                    whitelist INTEGER NOT NULL DEFAULT 0,
                    access_override TEXT NOT NULL DEFAULT 'none',
                    manual_access_until TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    provider TEXT NOT NULL,
                    provider_subscription_id TEXT,
                    provider_customer_id TEXT,
                    billing_status TEXT NOT NULL DEFAULT 'pending',
                    payment_status TEXT NOT NULL DEFAULT 'pending',
                    provider_paid_until TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider, provider_subscription_id)
                );

                CREATE TABLE IF NOT EXISTS inbox_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    external_event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT,
                    UNIQUE(provider, external_event_id)
                );

                CREATE TABLE IF NOT EXISTS outbox_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    aggregate_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT,
                    UNIQUE(kind, aggregate_key)
                );

                CREATE TABLE IF NOT EXISTS sheets_commands (
                    command_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    requested_by TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    result TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    user_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS app_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_key_id TEXT UNIQUE,
                    app_name TEXT NOT NULL,
                    access_plan TEXT,
                    encrypted_key TEXT NOT NULL,
                    key_expires_at TEXT,
                    assigned_user_id INTEGER REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'issued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    issued_at TEXT,
                    revoked_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS support_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    message_text TEXT,
                    status TEXT NOT NULL DEFAULT 'awaiting_message',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    notification_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, notification_key, channel)
                );

                CREATE TABLE IF NOT EXISTS stripe_checkout_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    subscription_id TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    chat_id TEXT NOT NULL,
                    invite_link TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    used_at TEXT
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
            if "wordpress_login" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN wordpress_login TEXT")
            if "telegram_membership_status" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN telegram_membership_status TEXT NOT NULL DEFAULT 'unknown'")
            if "telegram_banned" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN telegram_banned INTEGER NOT NULL DEFAULT 0")
            if "telegram_ban_source" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN telegram_ban_source TEXT")

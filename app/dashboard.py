from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any

from .access import effective_access


HEADERS = [
    "user_id", "telegram_id", "username", "wordpress_email", "wordpress_role",
    "access", "provider", "provider_paid_until", "manual_access_until", "whitelist",
    "access_override", "updated_at",
]


def rows_for_dashboard(db: sqlite3.Connection) -> list[list[Any]]:
    rows: list[list[Any]] = [HEADERS]
    users = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    for user in users:
        subscription = db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user["id"],)
        ).fetchone()
        rows.append([
            user["id"], user["telegram_id"], user["telegram_username"] or "",
            user["wordpress_email"] or "", user["wordpress_role"] or "",
            effective_access(user, subscription), subscription["provider"] if subscription else "",
            subscription["provider_paid_until"] if subscription else "", user["manual_access_until"] or "",
            "yes" if user["whitelist"] else "no", user["access_override"], user["updated_at"],
        ])
    return rows


def rows_as_csv(rows: list[list[Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return output.getvalue()

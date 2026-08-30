from __future__ import annotations

import sqlite3


DEFAULT_FLAGS = {
    "telegram_group_removal": True,
    "telegram_channel_removal": True,
    "wordpress_deactivation": True,
    "wordpress_access": True,
    "app_keys": True,
    "reminders": True,
}


def get_flags(db: sqlite3.Connection) -> dict[str, bool]:
    rows = db.execute("SELECT name, enabled FROM feature_flags").fetchall()
    flags = DEFAULT_FLAGS.copy()
    for row in rows:
        if row["name"] in flags:
            flags[row["name"]] = bool(row["enabled"])
    return flags


def sync_flags(db: sqlite3.Connection, rows: list[dict]) -> dict[str, int]:
    updated = 0
    for row in rows:
        name = str(row.get("name") or "").strip()
        if name not in DEFAULT_FLAGS:
            raise ValueError(f"unknown feature flag: {name}")
        value = row.get("enabled")
        enabled = value is True or str(value).strip().lower() in {"true", "yes", "on", "1", "вкл"}
        db.execute(
            "INSERT INTO feature_flags(name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled, updated_at = CURRENT_TIMESTAMP",
            (name, int(enabled)),
        )
        updated += 1
    return {"updated": updated}

"""Explicit, audited profile edits from the Sheets panel; no payment imports."""
from datetime import datetime, time, timezone
import json
import sqlite3
from typing import Any


def edit_sheet_user(db: sqlite3.Connection, telegram_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    if not 0 < telegram_id < 2**53:
        raise ValueError('Некорректный Telegram ID')
    allowed = {'wordpress_email', 'manual_access_until'}
    if not payload or set(payload) - allowed:
        raise ValueError('Укажите email или ручной срок доступа. Оплаченный срок меняется через платежи.')
    changes: dict[str, str] = {}
    if 'wordpress_email' in payload:
        email = payload['wordpress_email']
        if not isinstance(email, str):
            raise ValueError('Введите email')
        email = email.strip()
        if len(email) > 254 or email.count('@') != 1 or not all(email.split('@')) or any(c.isspace() for c in email):
            raise ValueError('Введите корректный email')
        changes['wordpress_email'] = email
    if 'manual_access_until' in payload:
        value = payload['manual_access_until']
        if not isinstance(value, str) or not value:
            raise ValueError('Выберите дату окончания ручного доступа')
        try:
            if len(value) == 10:
                parsed = datetime.combine(datetime.strptime(value, '%Y-%m-%d').date(), time(23, 59, 59), timezone.utc)
            else:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError('timezone required')
            changes['manual_access_until'] = parsed.astimezone(timezone.utc).isoformat()
        except ValueError as exc:
            raise ValueError('Дата должна быть YYYY-MM-DD или ISO-8601 с часовым поясом') from exc
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    user = db.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,)).fetchone()
    if not user:
        raise LookupError('Пользователь с таким Telegram ID не найден')
    before = {key: user[key] for key in changes}
    # Column names come only from the fixed allowlist above.
    assignments = ', '.join(f'{key} = ?' for key in changes)
    db.execute(f'UPDATE users SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (*changes.values(), user['id']))
    db.execute(
        "INSERT INTO audit_log(actor, action, user_id, details) VALUES (?, 'sheets.edit_user', ?, ?)",
        (actor, user['id'], json.dumps({'before': before, 'after': changes}, ensure_ascii=False)),
    )
    return {'telegram_id': telegram_id, 'user_id': user['id'], **changes}

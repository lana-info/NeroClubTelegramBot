"""Check real assigned keys through the bot handler without Telegram delivery.

The production database is opened read-only and copied into memory. Keys and
captured responses stay inside the bot container; only pass/fail metadata leaves it.
Run this along with check_license_registry.py after changing key assignments.
"""

import argparse
import json
import subprocess


CHECK = r'''
import asyncio, json, sqlite3
from datetime import datetime, timezone
from app.config import settings
from app.db import Database
from app.feature_flags import get_flags
from app.keys import decrypt_key
from app.telegram_updates import process_update

class CaptureOnly:
    def __init__(self):
        self.messages = []
    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

with Database(settings.database_url).connect() as source:
    source.execute('PRAGMA query_only=ON')
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    source.backup(db)
flags = get_flags(db)
now = datetime.now(timezone.utc)
owners = db.execute("SELECT DISTINCT u.* FROM users u JOIN app_keys k ON k.assigned_user_id=u.id WHERE k.status='issued' ORDER BY u.id").fetchall()
next_update = int(db.execute("SELECT COALESCE(MAX(CAST(external_event_id AS INTEGER)),0)+1 FROM inbox_events WHERE provider='telegram'").fetchone()[0])
checks = 0
failures = []
key_count = 0
for owner in owners:
    rows = db.execute("SELECT * FROM app_keys WHERE assigned_user_id=? AND status='issued'", (owner['id'],)).fetchall()
    expected = []
    for row in rows:
        expiry = row['key_expires_at']
        if expiry:
            expiry = datetime.fromisoformat(expiry.replace('Z','+00:00'))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= now:
                continue
        expected.append((row['app_name'], decrypt_key(row['encrypted_key'], settings.app_keys_encryption_key)))
    key_count += len(expected)
    for scenario in ('current', 'subscription_expired'):
        if scenario == 'subscription_expired':
            db.execute("UPDATE subscriptions SET provider_paid_until='2000-01-01T00:00:00+00:00' WHERE user_id=?", (owner['id'],))
            db.execute("UPDATE users SET whitelist=0, access_override='none', manual_access_until=NULL WHERE id=?", (owner['id'],))
        for command in ('/my-keys', '/my_keys', '/my_keys@Etsy_NeuroClub_Bot', '🔑 Мои ключи'):
            capture = CaptureOnly()
            update = {'update_id':next_update,'message':{'chat':{'id':owner['telegram_id'],'type':'private'},'from':{'id':owner['telegram_id']},'text':command}}
            next_update += 1
            result = asyncio.run(process_update(db, update, capture, app_keys_encryption_key=settings.app_keys_encryption_key, feature_flags=flags))
            checks += 1
            ok = result == 'processed' and len(capture.messages) == 1
            if ok:
                recipient, text = capture.messages[0]
                ok = recipient == owner['telegram_id'] and text.count('Ключ: ') == len(expected)
                ok = ok and all(app_name in text and secret in text for app_name, secret in expected)
            if not ok:
                failures.append({'telegram_id':owner['telegram_id'],'scenario':scenario,'command':command,'expected_keys':len(expected)})
print(json.dumps({'owners':len(owners),'keys':key_count,'handler_checks':checks,'failures':failures},ensure_ascii=False))
db.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bot-container', default='nero-club-sheet-payment-bot-backend-1')
    args = parser.parse_args()
    try:
        completed = subprocess.run(
            ['docker', 'exec', '-i', args.bot_container, 'python', '-c', CHECK],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        if completed.returncode:
            raise RuntimeError('Container check failed')
        result = json.loads(completed.stdout)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'check_complete':False, 'error_type':type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

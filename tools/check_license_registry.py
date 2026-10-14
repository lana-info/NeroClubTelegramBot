"""Read-only check of encrypted bot keys against the license registry.

Run on the Docker host after changing either registry. Plaintext keys stay
inside the bot; only suffixes and salted verifiers cross container boundaries.
This detects registry drift, but does not replace activation/client tests.
"""

import argparse
import json
import subprocess
import sys


EXPORT = """
import json
from app.config import settings
from app.db import Database
from app.keys import decrypt_key
with Database(settings.database_url).connect() as db:
    db.execute('PRAGMA query_only=ON')
    rows = []
    for key in db.execute("SELECT * FROM app_keys WHERE status='issued'"):
        user = db.execute('SELECT telegram_id FROM users WHERE id=?', (key['assigned_user_id'],)).fetchone()
        raw = decrypt_key(key['encrypted_key'], settings.app_keys_encryption_key).strip().upper()
        rows.append({'external_key_id': key['external_key_id'], 'telegram_id': user['telegram_id'] if user else None,
                     'app_name': key['app_name'], 'suffix': raw[-8:]})
    print(json.dumps(rows))
"""

REGISTRY = """
import json, sys, psycopg
from psycopg.rows import dict_row
from license_service.config import ServiceConfig
items = json.load(sys.stdin)
cfg = ServiceConfig.from_environment()
result = []
with psycopg.connect(cfg.database_url, row_factory=dict_row) as db:
    db.execute('SET TRANSACTION READ ONLY')
    for item in items:
        matches = db.execute('''SELECT l.id, p.code AS product_id, l.status, l.starts_at, l.expires_at,
                                       l.key_salt, l.key_verifier
                                FROM licenses l JOIN products p ON p.id=l.product_id
                                WHERE l.key_suffix=%s''', (item['suffix'],)).fetchall()
        candidates = [{'license_id': str(r['id']), 'product_id': r['product_id'], 'status': r['status'],
                       'starts_at': r['starts_at'], 'expires_at': r['expires_at'],
                       'salt': bytes(r['key_salt']).hex(), 'verifier': bytes(r['key_verifier']).hex()} for r in matches]
        result.append({**item, 'candidates': candidates})
print(json.dumps(result, default=str))
"""

COMPARE = """
import json, sys, hmac, hashlib
from datetime import datetime, timezone
from app.config import settings
from app.db import Database
from app.keys import decrypt_key
aliases = {'clipart-generator': 'clipart-generator', 'Clipart Generator': 'clipart-generator',
           'Vibix Images': 'clipart-generator', 'Vibix Studio': 'clipart-generator',
           'trendwatcher-video': 'trendwatcher-video', 'TrendWatcher Video': 'trendwatcher-video',
           'Vibix Video': 'trendwatcher-video'}
records = json.load(sys.stdin)
now = datetime.now(timezone.utc)
results = []
with Database(settings.database_url).connect() as db:
    db.execute('PRAGMA query_only=ON')
    for item in records:
        key = db.execute('SELECT * FROM app_keys WHERE external_key_id=?', (item['external_key_id'],)).fetchone()
        assert key and key['status'] == 'issued', 'Registry changed during check; retry'
        raw = decrypt_key(key['encrypted_key'], settings.app_keys_encryption_key).strip().upper().encode('ascii')
        matches = []
        for candidate in item['candidates']:
            salt = bytes.fromhex(candidate.pop('salt'))
            verifier = bytes.fromhex(candidate.pop('verifier'))
            if hmac.compare_digest(hashlib.scrypt(raw, salt=salt, n=2**14, r=8, p=1, dklen=32), verifier):
                matches.append(candidate)
        matched = len(matches) == 1
        eligible = matched and matches[0]['status'] == 'active'
        if eligible:
            eligible = datetime.fromisoformat(matches[0]['starts_at']) <= now
            expiry = matches[0]['expires_at']
            eligible = eligible and (not expiry or datetime.fromisoformat(expiry) > now)
        product_matches = matched and matches[0]['product_id'] == aliases.get(item['app_name'])
        ok = bool(matched and eligible and product_matches and item['telegram_id'] is not None)
        results.append({'external_key_id': item['external_key_id'], 'telegram_id': item['telegram_id'],
                        'ok': ok, 'key_matches': matched, 'license_eligible': bool(eligible),
                        'product_matches': bool(product_matches)})
print(json.dumps(results))
"""


def run(container, source, payload=None):
    result = subprocess.run(
        ['docker', 'exec', '-i', container, 'python', '-c', source],
        input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    if result.returncode:
        # Do not print remote stderr or intermediate credential metadata.
        raise RuntimeError(f'Container check failed: {container}, exit {result.returncode}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bot-container', default='nero-club-sheet-payment-bot-backend-1')
    parser.add_argument('--license-container', default='license-service-license-service-1')
    args = parser.parse_args()
    try:
        suffixes = run(args.bot_container, EXPORT)
        verifiers = run(args.license_container, REGISTRY, suffixes)
        results = json.loads(run(args.bot_container, COMPARE, verifiers))
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'check_complete': False}))
        return 2
    failures = [r for r in results if not r['ok']]
    print(json.dumps({'checked': len(results), 'matched': len(results) - len(failures), 'failures': failures}))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

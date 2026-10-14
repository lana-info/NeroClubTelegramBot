from dataclasses import replace
import asyncio
import importlib

import httpx
import pytest

from app.access import upsert_user
from app.db import Database


@pytest.fixture
def panel(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 'edits.db'}")
    db.init_schema()
    main = importlib.import_module('app.main')
    monkeypatch.setattr(main, 'db', db)
    monkeypatch.setattr(main, 'settings', replace(main.settings, admin_api_token='test-admin'))
    with db.connect() as connection:
        upsert_user(connection, {'telegram_id': 42, 'wordpress_email': 'old@example.org'})
    return main.app, db


def request(panel, method, path, payload=None, authorized=True):
    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel[0]), base_url='http://test') as client:
            return await client.request(method, path, json=payload, headers={'Authorization': 'Bearer test-admin'} if authorized else {})
    return asyncio.run(send())


def test_email_and_manual_date_survive_export_and_preserve_payments(panel):
    _, db = panel
    with db.connect() as connection:
        connection.execute("INSERT INTO subscriptions(user_id, provider, provider_subscription_id, payment_status, provider_paid_until) VALUES (1, 'sheet', 'existing', 'paid', '2099-01-01T00:00:00+00:00')")
    response = request(panel, 'PATCH', '/internal/sheets/users/42', {'wordpress_email': ' new@example.org ', 'manual_access_until': '2099-02-03'})
    assert response.status_code == 200, response.text
    for _ in range(2):
        rows = request(panel, 'GET', '/internal/sheets/users').json()['rows']
        assert rows[0][3] == 'new@example.org'
        assert rows[0][7] == '2099-01-01T00:00:00+00:00'
        assert rows[0][8] == '2099-02-03T23:59:59+00:00'
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_log WHERE action='sheets.edit_user'").fetchone()[0] == 1


def test_manual_access_works_without_payment_and_honors_denial(panel):
    response = request(panel, 'PATCH', '/internal/sheets/users/42', {'manual_access_until': '2099-02-03'})
    assert response.status_code == 200, response.text
    assert request(panel, 'GET', '/internal/sheets/users').json()['rows'][0][5] == 'active'
    with panel[1].connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 0
        connection.execute("UPDATE users SET access_override='deny'")
    assert request(panel, 'GET', '/internal/sheets/users').json()['rows'][0][5] == 'denied'
    with panel[1].connect() as connection:
        connection.execute("UPDATE users SET access_override='none', telegram_banned=1")
    assert request(panel, 'GET', '/internal/sheets/users').json()['rows'][0][5] == 'denied'


@pytest.mark.parametrize('payload', [
    {}, {'wordpress_email': 'bad'}, {'wordpress_email': ''},
    {'wordpress_email': 'a@b c'}, {'wordpress_email': None},
    {'wordpress_email': 'new@example.org', 'manual_access_until': '2026-02-30'},
    {'manual_access_until': None}, {'manual_access_until': 'tomorrow'},
    {'manual_access_until': '2099-01-01T12:00:00'},
    {'provider_paid_until': '2099-01-01'}, {'access_override': 'allow'},
])
def test_invalid_edits_do_not_change_profile_or_audit(panel, payload):
    assert request(panel, 'PATCH', '/internal/sheets/users/42', payload).status_code == 422
    with panel[1].connect() as connection:
        row = connection.execute('SELECT * FROM users').fetchone()
        assert row['wordpress_email'] == 'old@example.org'
        assert row['manual_access_until'] is None
        assert connection.execute('SELECT COUNT(*) FROM audit_log').fetchone()[0] == 0


def test_partial_edit_does_not_erase_other_fields_and_requires_auth(panel):
    assert request(panel, 'PATCH', '/internal/sheets/users/42', {'wordpress_email': 'new@example.org'}, authorized=False).status_code == 401
    assert request(panel, 'PATCH', '/internal/sheets/users/99', {'wordpress_email': 'new@example.org'}).status_code == 404
    assert request(panel, 'PATCH', '/internal/sheets/users/42', {'manual_access_until': '2099-01-01'}).status_code == 200
    assert request(panel, 'PATCH', '/internal/sheets/users/42', {'wordpress_email': 'new@example.org'}).status_code == 200
    row = request(panel, 'GET', '/internal/sheets/users').json()['rows'][0]
    assert row[3] == 'new@example.org'
    assert row[8] == '2099-01-01T23:59:59+00:00'

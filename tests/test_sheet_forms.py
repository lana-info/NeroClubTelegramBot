from dataclasses import replace
import importlib

import pytest
from fastapi.testclient import TestClient

from app.db import Database


@pytest.fixture
def panel(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'forms.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    main = importlib.import_module("app.main")
    db = Database(url)
    db.init_schema()
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "settings", replace(main.settings, admin_api_token="test-admin"))
    with TestClient(main.app) as client:
        client.headers["Authorization"] = "Bearer test-admin"
        yield client, db


def add_payment(client, key, date="2026-01-15"):
    response = client.post('/internal/sheets/payments', json={"payments": [{
        "payment_id": key, "telegram_id": 42, "paid_at": date,
        "payment_provider": "stripe", "amount_usd": 20, "provider_payment_id": key,
    }]})
    assert response.status_code == 200
    assert response.json()['payments'][0]['status'] == 'processed'


def deletion(client, key, **changes):
    return client.post('/internal/sheets/payment-deletions', json={
        "payment_id": key, "telegram_id": "42", "note": "Ошибочный ввод", "confirmed": True, **changes,
    })


def test_create_user_then_payment_and_duplicate_preserves_profile(panel):
    client, db = panel
    response = client.post('/internal/sheets/users', json={
        'telegram_id': 42, 'username': '@anna', 'wordpress_email': 'anna@example.org',
    })
    assert response.status_code == 201
    rows = client.get('/internal/sheets/users').json()['rows']
    assert rows[0][1:6] == [42, 'anna', 'anna@example.org', '', 'denied']
    assert client.post('/internal/sheets/users', json={'telegram_id': 42, 'username': 'other'}).status_code == 422
    with db.connect() as connection:
        assert connection.execute('SELECT telegram_username FROM users').fetchone()[0] == 'anna'
        assert connection.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 0
    add_payment(client, 'first')


@pytest.mark.parametrize('telegram_id', [True, 0, -42, '42', 1.5, 2**53])
def test_create_user_rejects_invalid_id(panel, telegram_id):
    client, _ = panel
    assert client.post('/internal/sheets/users', json={'telegram_id': telegram_id}).status_code == 422


def test_delete_middle_payment_recalculates_later_payments_and_is_idempotent(panel):
    client, db = panel
    client.post('/internal/sheets/users', json={'telegram_id': 42})
    for key in ['first', 'middle', 'last']:
        add_payment(client, key)
    assert deletion(client, 'middle').status_code == 200
    rows = client.get('/internal/sheets/payments').json()['rows']
    assert [(row[0], row[5]) for row in rows] == [('first', '2026-02-15'), ('last', '2026-03-15')]
    assert deletion(client, 'middle').status_code == 200
    repeat = client.post('/internal/sheets/payments', json={'payments': [{
        'payment_id': 'middle', 'telegram_id': 42, 'paid_at': '2026-01-15',
    }]}).json()['payments'][0]
    assert repeat['status'] == 'duplicate'
    with db.connect() as connection:
        assert connection.execute('SELECT provider_paid_until FROM subscriptions').fetchone()[0] == '2026-03-15T00:00:00+00:00'
        assert connection.execute('SELECT COUNT(*) FROM sheet_payment_corrections').fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'sheets.delete_payment'").fetchone()[0] == 1
    assert client.get('/internal/sheets/payment-corrections').json()['rows'][0][5] == 'Удаление: Ошибочный ввод'


@pytest.mark.parametrize('baseline', [None, '2026-01-31T00:00:00+00:00'])
def test_delete_only_payment_restores_exact_baseline_and_whitelist(panel, baseline):
    client, db = panel
    user_id = client.post('/internal/sheets/users', json={'telegram_id': 42}).json()['user']['user_id']
    if baseline:
        with db.connect() as connection:
            connection.execute("INSERT INTO subscriptions(user_id, provider, provider_subscription_id, payment_status, provider_paid_until) VALUES (?, 'sheet', 'manual-42', 'paid', ?)", (user_id, baseline))
    add_payment(client, 'only')
    assert deletion(client, 'only').status_code == 200
    assert client.get('/internal/sheets/payments').json()['rows'] == []
    assert client.get('/internal/sheets/users').json()['rows'][0][5] == 'denied'
    with db.connect() as connection:
        assert connection.execute('SELECT provider_paid_until FROM subscriptions').fetchone()[0] == baseline
    client.post('/internal/sheets/whitelist', json={'users': [{'user_id': user_id, 'whitelist': 'yes'}]})
    assert client.get('/internal/sheets/users').json()['rows'][0][5] == 'active'
    add_payment(client, 'new', '2026-05-31')
    assert client.get('/internal/sheets/payments').json()['rows'][0][5] == '2026-06-30'


@pytest.mark.parametrize('changes', [{'telegram_id': 99}, {'confirmed': False}, {'note': ''}])
def test_deletion_validation_leaves_payment_unchanged(panel, changes):
    client, _ = panel
    client.post('/internal/sheets/users', json={'telegram_id': 42})
    add_payment(client, 'keep')
    assert deletion(client, 'keep', **changes).status_code == 422
    assert client.get('/internal/sheets/payments').json()['rows'][0][4:6] == ['processed', '2026-02-15']


def test_new_endpoints_require_admin(panel):
    client, _ = panel
    client.headers.pop('Authorization')
    assert client.post('/internal/sheets/users', json={'telegram_id': 42}).status_code == 401
    assert deletion(client, 'missing').status_code == 401


def test_legacy_ambiguous_baseline_blocks_deletion_without_changes(panel):
    client, db = panel
    user_id = client.post('/internal/sheets/users', json={'telegram_id': 42}).json()['user']['user_id']
    with db.connect() as connection:
        connection.execute("INSERT INTO subscriptions(user_id, provider, provider_subscription_id, payment_status, provider_paid_until) VALUES (?, 'sheet', 'manual-42', 'paid', '2026-01-31T00:00:00+00:00')", (user_id,))
    add_payment(client, 'legacy')
    with db.connect() as connection:
        connection.execute('DELETE FROM sheet_payment_baselines')
    assert deletion(client, 'legacy').status_code == 422
    assert client.get('/internal/sheets/payments').json()['rows'][0][4:6] == ['processed', '2026-02-28']


def test_legacy_imported_balance_is_preserved(panel):
    client, db = panel
    user_id = client.post('/internal/sheets/users', json={'telegram_id': 42}).json()['user']['user_id']
    with db.connect() as connection:
        connection.execute("INSERT INTO subscriptions(user_id, provider, provider_subscription_id, payment_status, provider_paid_until) VALUES (?, 'legacy', 'legacy-42', 'paid', '2026-01-31T00:00:00+00:00')", (user_id,))
    add_payment(client, 'legacy')
    with db.connect() as connection:
        connection.execute('DELETE FROM sheet_payment_baselines')
    assert deletion(client, 'legacy').status_code == 200
    with db.connect() as connection:
        assert [row[0] for row in connection.execute('SELECT provider_paid_until FROM subscriptions')] == ['2026-01-31T00:00:00+00:00'] * 2

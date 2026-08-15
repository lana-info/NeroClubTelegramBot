import hashlib
import hmac
import json
import time
import asyncio

from app.access import apply_command, effective_access, upsert_user
from app.db import Database
from app.webhooks import parse_event, verify_stripe_signature
from app.stripe_events import apply_stripe_event, process_pending_stripe_events
from app.dashboard import rows_as_csv, rows_for_dashboard
from app.site_access import process_pending_site_access_jobs
from app.keys import create_app_key, keys_for_user
from cryptography.fernet import Fernet


def database(tmp_path) -> Database:
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_schema()
    return db


def signed(payload: bytes, secret: str) -> str:
    timestamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_schema_and_health_storage(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        user = upsert_user(connection, {"telegram_id": 123, "telegram_username": "anna"})
        assert user["telegram_id"] == 123


def test_sheet_command_is_idempotent(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 456})
        command = {"command_id": "cmd-1", "user_id": user["id"], "action": "whitelist"}
        first = apply_command(connection, **command, payload={}, actor="test-admin")
        second = apply_command(connection, **command, payload={}, actor="test-admin")
        assert first["status"] == "done"
        assert second["status"] == "done"
        stored = connection.execute("SELECT whitelist FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert stored["whitelist"] == 1


def test_sheet_credentials_command_is_queued_without_password(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 456})
        result = apply_command(connection, "cred-1", user["id"], "issue_credentials", {}, "test-admin")
        assert result["status"] == "queued"
        job = connection.execute("SELECT kind, payload FROM outbox_jobs").fetchone()
        assert job["kind"] == "site.credentials"
        assert "password" not in job["payload"].lower()


def test_site_access_job_delivers_and_completes_sheet_command(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))

    class FakeWordPress:
        async def sync_user(self, payload, idempotency_key):
            self.payload = payload
            self.idempotency_key = idempotency_key
            return {"user_id": 9, "login": "anna"}

    db = database(tmp_path)
    telegram = FakeTelegram()
    wordpress = FakeWordPress()
    with db.connect() as connection:
        user = upsert_user(connection, {
            "telegram_id": 456, "wordpress_email": "anna@example.com", "wordpress_login": "anna",
        })
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub_1', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        apply_command(connection, "cred-2", user["id"], "issue_credentials", {}, "test-admin")
        result = asyncio.run(process_pending_site_access_jobs(connection, telegram, wordpress))
        assert result == {"processed": 1, "failed": 0}
        assert telegram.messages[0][0] == 456
        assert "Постоянный пароль:" in telegram.messages[0][1]
        stored = json.dumps(dict(connection.execute("SELECT payload, result FROM sheets_commands").fetchone()))
        assert wordpress.payload["password"] not in stored
        assert connection.execute("SELECT status FROM sheets_commands").fetchone()[0] == "done"


def test_app_key_is_encrypted_and_respects_expiry(tmp_path):
    db = database(tmp_path)
    encryption_key = Fernet.generate_key().decode()
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 999})
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub_keys', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        create_app_key(connection, {
            "key_id": "app-1", "app_name": "Example App", "key": "secret-value",
            "user_id": user["id"], "key_expires_at": "2999-01-01T00:00:00+00:00",
        }, encryption_key)
        row = connection.execute("SELECT encrypted_key FROM app_keys").fetchone()
        assert row["encrypted_key"] != "secret-value"
        assert keys_for_user(connection, user["id"], encryption_key)[0]["key"] == "secret-value"


def test_access_override_and_whitelist(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 789})
        assert effective_access(user, None) == "denied"
        apply_command(connection, "allow-1", user["id"], "allow", {}, "test-admin")
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert effective_access(user, None) == "active"
        apply_command(connection, "deny-1", user["id"], "deny", {}, "test-admin")
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert effective_access(user, None) == "denied"


def test_stripe_signature_and_event_validation():
    payload = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"object": {}}}).encode()
    verify_stripe_signature(payload, signed(payload, "whsec_test"), "whsec_test", 300)
    assert parse_event(payload)["id"] == "evt_1"


def test_stripe_signature_rejects_bad_signature():
    payload = b'{"id":"evt_bad","type":"invoice.paid"}'
    try:
        verify_stripe_signature(payload, "t=1,v1=bad", "whsec_test", 300)
    except ValueError as exc:
        assert "timestamp" in str(exc)
    else:
        raise AssertionError("bad signature was accepted")


def test_stripe_event_updates_subscription_and_is_safe_when_replayed(tmp_path):
    db = database(tmp_path)
    event = {
        "id": "evt_paid",
        "type": "invoice.paid",
        "data": {"object": {
            "subscription": "sub_1", "customer": "cus_1", "period_end": 4102444800,
            "metadata": {"internal_user_id": "1"},
        }},
    }
    with db.connect() as connection:
        user = connection.execute("INSERT INTO users(telegram_id) VALUES (123) RETURNING id").fetchone()
        event["data"]["object"]["metadata"]["internal_user_id"] = str(user["id"])
        assert apply_stripe_event(connection, event) == "processed"
        assert apply_stripe_event(connection, event) == "processed"
        subscription = connection.execute("SELECT * FROM subscriptions").fetchall()
        assert len(subscription) == 1
        assert subscription[0]["payment_status"] == "paid"


def test_pending_stripe_job_marks_unknown_user_failed(tmp_path):
    db = database(tmp_path)
    event = {"id": "evt_unknown", "type": "invoice.paid", "data": {"object": {
        "subscription": "sub_unknown", "metadata": {"internal_user_id": "999"},
    }}}
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO inbox_events(provider, external_event_id, event_type, payload) VALUES (?, ?, ?, ?)",
            ("stripe", event["id"], event["type"], json.dumps(event)),
        )
        connection.execute(
            "INSERT INTO outbox_jobs(kind, aggregate_key, payload) VALUES (?, ?, ?)",
            ("stripe.event", event["id"], json.dumps(event)),
        )
        assert process_pending_stripe_events(connection) == {"processed": 0, "ignored": 0, "failed": 1}
        assert connection.execute("SELECT status FROM outbox_jobs").fetchone()["status"] == "failed"


def test_dashboard_projection_contains_status_but_no_password(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO users(telegram_id, telegram_username, wordpress_email) VALUES (?, ?, ?)",
            (123, "anna", "anna@example.com"),
        )
        rows = rows_for_dashboard(connection)
    csv_text = rows_as_csv(rows)
    assert "telegram_id" in csv_text
    assert "anna@example.com" in csv_text
    assert "password" not in csv_text.lower()

import hashlib
import hmac
import json
import time

from app.access import apply_command, effective_access, upsert_user
from app.db import Database
from app.webhooks import parse_event, verify_stripe_signature


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

import hashlib
import hmac
import json
import time
import asyncio
import httpx
from datetime import datetime

from app.access import apply_command, effective_access, upsert_user
from app.db import Database
from app.webhooks import parse_event, verify_stripe_signature
from app.stripe_events import apply_stripe_event, process_pending_stripe_events
from app.dashboard import rows_as_csv, rows_for_dashboard
from app.site_access import process_pending_site_access_jobs, queue_site_access_job
from app.membership import (
    create_personal_invite,
    process_pending_telegram_invite_jobs,
    process_pending_telegram_restore_jobs,
    reconcile_members,
)
from app.stripe_checkout import create_checkout_session
from app.reminders import send_subscription_reminders
from app.keys import create_app_key, keys_for_user, sync_app_key_rows
from app.sheets import dashboard_rows, import_users, rows_for_site_access_sheet, rows_for_users_sheet
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
        assert "Первоначальный пароль:" in telegram.messages[0][1]
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


def test_sheet_key_sync_issues_and_revokes_without_returning_secret(tmp_path):
    db = database(tmp_path)
    encryption_key = Fernet.generate_key().decode()
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 1000})
        result = sync_app_key_rows(connection, [{
            "key_id": "app-sync", "app_name": "Sync App", "key": "hidden-secret",
            "assigned_user_id": 1000, "status": "issued", "action": "issue",
        }], encryption_key)
        assert result == {"synced": 1, "revoked": 0, "errors": []}
        assert "hidden-secret" not in json.dumps(result)
        assert keys_for_user(connection, user["id"], encryption_key)[0]["key"] == "hidden-secret"
        result = sync_app_key_rows(connection, [{"key_id": "app-sync", "action": "revoke"}], encryption_key)
        assert result == {"synced": 0, "revoked": 1, "errors": []}


def test_sheet_snapshot_import_is_idempotent_and_exposes_operational_rows(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        payload = [{
            "telegram_id": 12345,
            "username": "@anna",
            "wordpress_email": "anna@example.com",
            "provider": "stripe",
            "provider_paid_until": "2999-01-01",
            "whitelist": "no",
            "access_override": "none",
        }]
        first = import_users(connection, payload)
        second = import_users(connection, payload)
        assert first == second
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1
        user_rows = rows_for_users_sheet(connection)
        site_rows = rows_for_site_access_sheet(connection)
        metrics = dashboard_rows(connection)
        assert user_rows[0][0:6] == ["user_id", "telegram_id", "username", "wordpress_email", "wordpress_role", "access"]
        assert user_rows[1][2] == "@anna"
        assert user_rows[1][5] == "active"
        assert site_rows[1][7] == "active"
        assert ["total_users", 1] in metrics


def test_site_access_sheet_matches_delivery_by_exact_user_id(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        first = upsert_user(connection, {"telegram_id": 1, "wordpress_email": "one@example.com"})
        second = upsert_user(connection, {"telegram_id": 10, "wordpress_email": "ten@example.com"})
        connection.execute(
            "INSERT INTO outbox_jobs(kind, aggregate_key, payload, status) VALUES ('site.credentials', 'delivery-10', ?, 'done')",
            (json.dumps({"user_id": second["id"]}),),
        )
        rows = rows_for_site_access_sheet(connection)
        first_row = next(row for row in rows[1:] if row[0] == first["id"])
        second_row = next(row for row in rows[1:] if row[0] == second["id"])
        assert first_row[8] == "not_requested"
        assert second_row[8] == "done"


def test_site_access_sheet_commands_queue_wordpress_jobs(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 12345, "wordpress_email": "anna@example.com"})
        revoke = apply_command(connection, "site-1", user["id"], "revoke_site_access", {}, "sheet")
        restore = apply_command(connection, "site-2", user["id"], "restore_site_access", {}, "sheet")
        assert revoke["status"] == "queued"
        assert restore["status"] == "queued"
        jobs = connection.execute("SELECT kind, payload FROM outbox_jobs ORDER BY id").fetchall()
        assert [job["kind"] for job in jobs] == ["site.deactivate", "site.restore"]
        assert '"command_id": "site-1"' in jobs[0]["payload"]


def test_membership_reconciliation_is_safe_in_dry_run(tmp_path):
    class FakeTelegram:
        async def get_chat_member(self, chat_id, user_id):
            return {"status": "member"}

        async def create_chat_invite_link(self, *args, **kwargs):
            raise AssertionError("dry-run must not call Telegram invite API")

        async def ban_chat_member(self, *args, **kwargs):
            raise AssertionError("dry-run must not remove members")

    db = database(tmp_path)
    with db.connect() as connection:
        active = upsert_user(connection, {"telegram_id": 1})
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub_active', 'active', 'paid', '2999-01-01T00:00:00+00:00')", (active["id"],)
        )
        upsert_user(connection, {"telegram_id": 2})
        result = asyncio.run(reconcile_members(connection, FakeTelegram(), "-100", dry_run=True))
        assert result == {"checked": 2, "active": 1, "denied": 1, "removed": 0, "would_remove": 1, "failed": 0}
        assert asyncio.run(create_personal_invite(connection, active["id"], FakeTelegram(), "-100", dry_run=True))["status"] == "dry_run"


def test_reconciliation_marks_automatic_ban_as_system_ban(tmp_path):
    class FakeTelegram:
        async def get_chat_member(self, chat_id, user_id):
            return {"status": "member"}

        async def ban_chat_member(self, chat_id, user_id):
            return {"status": "ok"}

    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 3})
        result = asyncio.run(reconcile_members(connection, FakeTelegram(), "-100", dry_run=False))
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert result["removed"] == 1
        assert stored["telegram_ban_source"] == "system"
        assert stored["telegram_banned"] == 0


def test_reconciliation_can_check_the_special_channel(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.checked = []
            self.banned = []

        async def get_chat_member(self, chat_id, user_id):
            self.checked.append((chat_id, user_id))
            return {"status": "member"}

        async def ban_chat_member(self, chat_id, user_id):
            self.banned.append((chat_id, user_id))
            return {"status": "ok"}

    db = database(tmp_path)
    telegram = FakeTelegram()
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 77})
        result = asyncio.run(reconcile_members(connection, telegram, "-100777", dry_run=False))

        assert result["removed"] == 1
        assert telegram.checked == [("-100777", 77)]
        assert telegram.banned == [("-100777", 77)]


def test_subscription_reminder_is_sent_once_for_seven_day_expiry(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))

    db = database(tmp_path)
    telegram = FakeTelegram()
    now = datetime.fromisoformat("2026-08-15T12:00:00+00:00")
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 44})
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub_reminder', 'active', 'paid', '2026-08-22T00:00:00+00:00')", (user["id"],)
        )
        result = asyncio.run(send_subscription_reminders(connection, telegram, payment_url="https://pay.test", now=now))
        assert result["sent"] == 1
        assert "Ссылка для оплаты: https://pay.test" in telegram.messages[0][1]
        assert "22 августа 2026 года" in telegram.messages[0][1]
        second = asyncio.run(send_subscription_reminders(connection, telegram, payment_url="https://pay.test", now=now))
        assert second["sent"] == 0
        assert second["skipped"] == 1


def test_access_override_and_whitelist(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 789})
        assert effective_access(user, None) == "denied"


def test_site_membership_jobs_call_restore_and_deactivate(tmp_path):
    class FakeTelegram:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("membership jobs must not message users")

    class FakeWordPress:
        def __init__(self):
            self.actions = []

        async def sync_user(self, payload, idempotency_key):
            self.actions.append((payload, idempotency_key))
            return {"user_id": 9, "login": "anna", "action": payload["action"]}

    db = database(tmp_path)
    wordpress = FakeWordPress()
    with db.connect() as connection:
        user = upsert_user(connection, {
            "telegram_id": 55, "wordpress_user_id": 9, "wordpress_email": "anna@example.com",
        })
        queue_site_access_job(connection, user["id"], "deactivate", "event-left")
        queue_site_access_job(connection, user["id"], "restore", "event-return")
        result = asyncio.run(process_pending_site_access_jobs(connection, FakeTelegram(), wordpress))
        assert result == {"processed": 2, "failed": 0}
    assert [item[0]["action"] for item in wordpress.actions] == ["deactivate", "restore"]


def test_site_membership_job_keeps_retry_state_on_wordpress_error(tmp_path):
    from app.integrations.wordpress import WordPressError

    class FakeTelegram:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("membership jobs must not message users")

    class FailingWordPress:
        async def sync_user(self, payload, idempotency_key):
            raise WordPressError("temporary failure")

    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 56, "wordpress_user_id": 10})
        queue_site_access_job(connection, user["id"], "deactivate", "event-failure")
        result = asyncio.run(process_pending_site_access_jobs(connection, FakeTelegram(), FailingWordPress()))
        job = connection.execute("SELECT status, attempts, last_error FROM outbox_jobs").fetchone()
        assert result == {"processed": 0, "failed": 1}
        assert job["status"] == "failed"
        assert job["attempts"] == 1
        assert "temporary failure" in job["last_error"]


def test_site_access_worker_dry_run_does_not_call_wordpress(tmp_path):
    class FakeTelegram:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("dry-run must not deliver site credentials")

    class FailingWordPress:
        async def sync_user(self, *args, **kwargs):
            raise AssertionError("dry-run must not call WordPress")

    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 57, "wordpress_user_id": 11})
        queue_site_access_job(connection, user["id"], "deactivate", "dry-run-deactivate")
        result = asyncio.run(process_pending_site_access_jobs(
            connection, FakeTelegram(), FailingWordPress(), dry_run=True
        ))
        assert result == {"processed": 0, "failed": 0, "skipped": 1}
        assert connection.execute("SELECT status FROM outbox_jobs").fetchone()[0] == "pending"


def test_restore_telegram_is_explicit_and_queues_wordpress_restore(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_user_id, telegram_banned, telegram_ban_source) VALUES (?, ?, 1, 'admin') RETURNING id",
            (66, 9),
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) VALUES (?, 'stripe', 'restore-sub', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        result = apply_command(connection, "restore-1", user["id"], "restore_telegram", {}, "test-admin")
        assert result["status"] == "queued"
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert stored["telegram_banned"] == 1
        assert stored["telegram_ban_source"] == "admin"
        assert connection.execute("SELECT kind FROM outbox_jobs").fetchone()[0] == "telegram.restore"


def test_telegram_restore_unbans_sends_invite_and_clears_manual_ban(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.calls = []

        async def unban_chat_member(self, chat_id, user_id):
            self.calls.append(("unban", chat_id, user_id))

        async def create_chat_invite_link(self, chat_id, **kwargs):
            self.calls.append(("invite", chat_id, kwargs))
            return {"invite_link": "https://t.me/+test"}

        async def send_message(self, chat_id, text):
            self.calls.append(("message", chat_id, text))

    db = database(tmp_path)
    telegram = FakeTelegram()
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, telegram_banned, telegram_ban_source) VALUES (?, 1, 'admin') RETURNING id",
            (67,),
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) VALUES (?, 'stripe', 'restore-sub-2', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        apply_command(connection, "restore-2", user["id"], "restore_telegram", {}, "test-admin")
        result = asyncio.run(process_pending_telegram_restore_jobs(connection, telegram, "-100", dry_run=False))
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert result == {"processed": 1, "failed": 0, "skipped": 0}
        assert stored["telegram_banned"] == 0
        assert stored["telegram_ban_source"] is None
        assert [call[0] for call in telegram.calls] == ["unban", "invite", "message"]
        apply_command(connection, "allow-1", user["id"], "allow", {}, "test-admin")
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert effective_access(user, None) == "active"
        apply_command(connection, "deny-1", user["id"], "deny", {}, "test-admin")
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert effective_access(user, None) == "denied"


def test_telegram_restore_unbans_and_invites_in_special_channel(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.calls = []

        async def unban_chat_member(self, chat_id, user_id):
            self.calls.append(("unban", chat_id, user_id))

        async def create_chat_invite_link(self, chat_id, **kwargs):
            self.calls.append(("invite", chat_id, kwargs))
            return {"invite_link": f"https://t.me/+{chat_id}"}

        async def send_message(self, chat_id, text):
            self.calls.append(("message", chat_id, text))

    db = database(tmp_path)
    telegram = FakeTelegram()
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, telegram_banned, telegram_ban_source) VALUES (?, 1, 'admin') RETURNING id",
            (88,),
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub-restore-channel', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        apply_command(connection, "restore-channel", user["id"], "restore_telegram", {}, "test-admin")
        result = asyncio.run(process_pending_telegram_restore_jobs(
            connection, telegram, "-100", additional_chat_ids=("-200",), dry_run=False
        ))

        assert result["processed"] == 1
        assert [(call[0], call[1]) for call in telegram.calls[:4]] == [
            ("unban", "-100"), ("invite", "-100"), ("unban", "-200"), ("invite", "-200")
        ]
        assert telegram.calls[4][0] == "message"
        assert "https://t.me/+-100" in telegram.calls[4][2]
        assert "https://t.me/+-200" in telegram.calls[4][2]


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
        assert connection.execute("SELECT kind FROM outbox_jobs").fetchone()[0] == "telegram.invite"


def test_stripe_checkout_session_contains_internal_user_metadata():
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "cs_test", "url": "https://checkout.test/cs_test"}, request=request)

    session = asyncio.run(create_checkout_session(
        "sk_test", "price_test", 42,
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
        customer_email="anna@example.com",
        transport=httpx.MockTransport(handler),
    ))
    assert session["url"].endswith("cs_test")
    body = requests[0].content.decode()
    assert "metadata%5Binternal_user_id%5D=42" in body
    assert "subscription_data%5Bmetadata%5D%5Binternal_user_id%5D=42" in body


def test_pending_telegram_invite_job_creates_link_only_for_non_member(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.messages = []
            self.links = []

        async def get_chat_member(self, chat_id, user_id):
            return {"status": "left"}

        async def create_chat_invite_link(self, chat_id, **kwargs):
            link = f"https://t.me/+{chat_id}-test"
            self.links.append((chat_id, kwargs))
            return {"invite_link": link}

        async def send_message(self, chat_id, text):
            self.messages.append((chat_id, text))

    db = database(tmp_path)
    telegram = FakeTelegram()
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 42})
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub-invite', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        connection.execute(
            "INSERT INTO outbox_jobs(kind, aggregate_key, payload) VALUES ('telegram.invite', 'invite-1', ?)",
            (json.dumps({"user_id": user["id"]}),),
        )
        result = asyncio.run(process_pending_telegram_invite_jobs(
            connection, telegram, ("-100", "-200"), dry_run=False
        ))
        assert result == {"processed": 1, "failed": 0, "skipped": 0}
        assert connection.execute("SELECT COUNT(*) FROM telegram_invites").fetchone()[0] == 2
        assert len(telegram.messages) == 1


def test_expired_telegram_invite_is_revoked(tmp_path):
    class FakeTelegram:
        def __init__(self):
            self.revoked = []

        async def revoke_chat_invite_link(self, chat_id, invite_link):
            self.revoked.append((chat_id, invite_link))

    db = database(tmp_path)
    telegram = FakeTelegram()
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 42})
        connection.execute(
            "INSERT INTO telegram_invites(user_id, chat_id, invite_link, expires_at) VALUES (?, '-100', ?, ?)",
            (user["id"], "https://t.me/+expired", "2000-01-01T00:00:00+00:00"),
        )
        from app.membership import revoke_expired_telegram_invites
        result = asyncio.run(revoke_expired_telegram_invites(connection, telegram, dry_run=False))
        assert result == {"revoked": 1, "would_revoke": 0, "failed": 0}
        assert connection.execute("SELECT status FROM telegram_invites").fetchone()[0] == "expired"
        assert telegram.revoked == [("-100", "https://t.me/+expired")]


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

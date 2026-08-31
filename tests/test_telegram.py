import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.fernet import Fernet

from app.db import Database
from app.integrations.telegram import TelegramClient
from app.telegram_updates import process_update
from app.keys import create_app_key


class TelegramTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}, request=request)


class WordPressTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.payload = None

    async def handle_async_request(self, request):
        self.payload = json.loads(request.content)
        return httpx.Response(200, json={"user_id": 9, "login": "anna"}, request=request)


def test_start_update_is_idempotent(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'telegram.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {"update_id": 10, "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "anna"}, "text": "/start"}}

    with db.connect() as connection:
        first = asyncio.run(process_update(connection, update, client))
    with db.connect() as connection:
        second = asyncio.run(process_update(connection, update, client))

    assert first == "processed"
    assert second == "duplicate"
    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == 42
    assert transport.calls[0]["reply_markup"]["is_persistent"] is True
    assert transport.calls[0]["reply_markup"]["keyboard"][0][0]["text"] == "📊 Статус подписки"


def test_webhook_setup_subscribes_to_chat_member_updates():
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    assert asyncio.run(client.set_webhook("https://example.test/webhooks/telegram", "secret"))
    assert transport.calls[0]["url"] == "https://example.test/webhooks/telegram"
    assert transport.calls[0]["secret_token"] == "secret"
    assert transport.calls[0]["allowed_updates"] == ["message", "chat_member", "chat_join_request"]


def test_friendly_menu_button_is_mapped_to_command(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'menu.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {"update_id": 13, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "🔑 Мои ключи"}}
    with db.connect() as connection:
        assert asyncio.run(process_update(connection, update, client)) == "processed"
    assert "ключей" in transport.calls[0]["text"].lower()


def test_status_uses_readable_russian_date(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'status-date.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {"update_id": 16, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/status"}}
    with db.connect() as connection:
        user = connection.execute("INSERT INTO users(telegram_id) VALUES (?) RETURNING id", (42,)).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) VALUES (?, 'sheet', 'sheet-42', 'active', 'paid', '2027-09-01T00:00:00+00:00')",
            (user["id"],),
        )
        asyncio.run(process_update(connection, update, client))
    assert "Доступ до: 1 сентября 2027 года" in transport.calls[0]["text"]


def test_user_can_toggle_reminders(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'reminders-toggle.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update_on = {"update_id": 17, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/reminders_on"}}
    update_off = {"update_id": 18, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "🔕 Выключить напоминания"}}
    update_toggle = {"update_id": 20, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "🔔 Уведомления об оплате"}}
    with db.connect() as connection:
        asyncio.run(process_update(connection, update_on, client))
        assert connection.execute("SELECT reminders_enabled FROM users WHERE telegram_id = 42").fetchone()[0] == 1
        asyncio.run(process_update(connection, update_off, client))
        assert connection.execute("SELECT reminders_enabled FROM users WHERE telegram_id = 42").fetchone()[0] == 0
        asyncio.run(process_update(connection, update_toggle, client))
        assert connection.execute("SELECT reminders_enabled FROM users WHERE telegram_id = 42").fetchone()[0] == 1
    assert "включены" in transport.calls[-1]["text"].lower()


def test_group_messages_do_not_receive_the_private_menu(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'group-messages.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {
        "update_id": 21,
        "message": {
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 42},
            "text": "/start",
        },
    }

    with db.connect() as connection:
        assert asyncio.run(process_update(connection, update, client)) == "ignored"
    assert transport.calls == []


def test_sheet_payment_source_does_not_start_stripe_checkout(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'sheet-payments.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {
        "update_id": 14,
        "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/report_payment"},
    }

    with db.connect() as connection:
        assert asyncio.run(process_update(
            connection,
            update,
            client,
            payment_source="sheet",
            stripe_secret_key="sk_test_should_not_be_used",
            stripe_price_id="price_should_not_be_used",
            admin_telegram_ids=(99,),
        )) == "processed"

    assert "Чтобы я нашла ваш платёж" in transport.calls[0]["text"]
    assert "email, который использовали при оплате" in transport.calls[0]["text"]
    confirmation = {
        "update_id": 15,
        "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "anna@example.com, 31 августа, Stripe"},
    }
    with db.connect() as connection:
        assert asyncio.run(process_update(connection, confirmation, client, admin_telegram_ids=(99,))) == "processed"
        assert connection.execute("SELECT status FROM support_requests").fetchone()[0] == "new"
    assert "Информация отправлена администратору" in transport.calls[-1]["text"]
    assert "Подтверждение оплаты" in transport.calls[-2]["text"]


def test_sheet_payment_links_use_new_member_price(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'payment-links.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {"update_id": 19, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/pay"}}

    with db.connect() as connection:
        asyncio.run(process_update(
            connection,
            update,
            client,
            payment_source="sheet",
            admin_telegram_ids=(99,),
            new_member_one_time_payment_url="https://pay.test/new-once",
            new_member_recurring_payment_url="https://pay.test/new-monthly",
        ))

    text = transport.calls[0]["text"]
    assert "Сумма к оплате: 20 USD" in text
    assert "https://pay.test/new-once" in text
    assert "https://pay.test/new-monthly" in text


def test_user_can_send_support_request_and_admin_can_reply(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'support.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    ask = {"update_id": 20, "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "anna"}, "text": "/support"}}
    message = {"update_id": 21, "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "anna"}, "text": "Не могу войти"}}
    reply = {"update_id": 22, "message": {"chat": {"id": 99}, "from": {"id": 99}, "text": "/reply 1 Попробуйте войти ещё раз"}}
    with db.connect() as connection:
        asyncio.run(process_update(connection, ask, client, admin_telegram_ids=(99,)))
        asyncio.run(process_update(connection, message, client, admin_telegram_ids=(99,)))
        assert transport.calls[1]["chat_id"] == 99
        assert "@anna" in transport.calls[1]["text"]
        assert "Сообщение отправлено" in transport.calls[2]["text"]
        asyncio.run(process_update(connection, reply, client, admin_telegram_ids=(99,)))
        assert connection.execute("SELECT status FROM support_requests WHERE id = 1").fetchone()[0] == "answered"
    assert any(call["chat_id"] == 42 and "Ответ администратора" in call["text"] for call in transport.calls)


def test_support_button_asks_for_message_before_sending(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'support-button.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {
        "update_id": 23,
        "message": {
            "chat": {"id": 42},
            "from": {"id": 42, "username": "anna"},
            "text": "💬 Связаться с администратором",
        },
    }

    with db.connect() as connection:
        asyncio.run(process_update(connection, update, client, admin_telegram_ids=(99,)))
        assert "Напишите следующим сообщением" in transport.calls[0]["text"]
        assert connection.execute("SELECT COUNT(*) FROM support_requests").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM support_requests").fetchone()[0] == "awaiting_message"


def test_unsolicited_private_message_is_not_forwarded_to_admin(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'unsolicited-message.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {
        "update_id": 24,
        "message": {
            "chat": {"id": 42},
            "from": {"id": 42, "username": "anna"},
            "text": "Привет, это обычное сообщение",
        },
    }

    with db.connect() as connection:
        assert asyncio.run(process_update(connection, update, client, admin_telegram_ids=(99,))) == "processed"

    assert transport.calls == []


def test_stale_support_prompt_does_not_capture_unsolicited_message(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'stale-support.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    update = {
        "update_id": 25,
        "message": {
            "chat": {"id": 42},
            "from": {"id": 42, "username": "anna"},
            "text": "Сообщение спустя много времени",
        },
    }

    with db.connect() as connection:
        user = connection.execute("INSERT INTO users(telegram_id) VALUES (?) RETURNING id", (42,)).fetchone()
        connection.execute(
            "INSERT INTO support_requests(user_id, status, updated_at) VALUES (?, 'awaiting_message', '2020-01-01 00:00:00')",
            (user["id"],),
        )
        assert asyncio.run(process_update(connection, update, client, admin_telegram_ids=(99,))) == "processed"

    assert transport.calls == []


def test_site_access_uses_active_subscription_and_does_not_store_password(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'site-access.db'}")
    db.init_schema()
    telegram_transport = TelegramTransport()
    wordpress_transport = WordPressTransport()
    telegram = TelegramClient("test-token", transport=telegram_transport)
    from app.integrations.wordpress import WordPressClient
    wordpress = WordPressClient("https://example.test", "shared-secret", transport=wordpress_transport)
    update = {"update_id": 11, "message": {"chat": {"id": 42}, "from": {"id": 42, "username": "anna"}, "text": "/site-access"}}
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_email) VALUES (?, ?) RETURNING id",
            (42, "anna@example.com"),
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) VALUES (?, 'stripe', 'sub_1', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        result = asyncio.run(process_update(connection, update, telegram, wordpress=wordpress))
        assert result == "processed"
        assert "password" not in json.dumps(dict(connection.execute("SELECT * FROM users").fetchone())).lower()
        assert connection.execute("SELECT site_credentials_delivered_at FROM users").fetchone()[0]
    assert wordpress_transport.payload["password"]
    assert "Первоначальный пароль" in telegram_transport.calls[0]["text"]
    assert "temporary_expires_at" not in wordpress_transport.payload


def test_my_keys_delivers_key_and_expiry_to_active_subscriber(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'keys.db'}")
    db.init_schema()
    telegram_transport = TelegramTransport()
    telegram = TelegramClient("test-token", transport=telegram_transport)
    encryption_key = Fernet.generate_key().decode()
    update = {"update_id": 12, "message": {"chat": {"id": 42}, "from": {"id": 42}, "text": "/my-keys"}}
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id) VALUES (?) RETURNING id", (42,)
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub_keys', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        create_app_key(connection, {
            "key_id": "app-1", "app_name": "Example App", "key": "secret-value",
            "user_id": user["id"], "key_expires_at": "2999-09-30T00:00:00+00:00",
        }, encryption_key)
        assert asyncio.run(process_update(connection, update, telegram, app_keys_encryption_key=encryption_key)) == "processed"
    assert "secret-value" in telegram_transport.calls[0]["text"]
    assert "30.09.2999" in telegram_transport.calls[0]["text"]


def _chat_member_update(update_id, telegram_id, status, chat_id=-100):
    return {
        "update_id": update_id,
        "chat_member": {
            "chat": {"id": chat_id},
            "new_chat_member": {"user": {"id": telegram_id}, "status": status},
        },
    }


def test_chat_member_left_queues_wordpress_deactivation(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'membership-events.db'}")
    db.init_schema()
    client = TelegramClient("test-token", transport=TelegramTransport())
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_user_id) VALUES (?, ?) RETURNING id", (42, 9)
        ).fetchone()
        assert asyncio.run(process_update(
            connection, _chat_member_update(100, 42, "left"), client, chat_id="-100"
        )) == "processed"
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        job = connection.execute("SELECT kind, payload FROM outbox_jobs").fetchone()
        assert stored["telegram_membership_status"] == "left"
        assert stored["telegram_banned"] == 0
        assert job["kind"] == "site.deactivate"
        assert json.loads(job["payload"])["user_id"] == user["id"]


def test_chat_member_return_restores_active_user_but_manual_ban_stays_blocked(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'membership-return.db'}")
    db.init_schema()
    client = TelegramClient("test-token", transport=TelegramTransport())
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_user_id, telegram_membership_status) VALUES (?, ?, 'left') RETURNING id",
            (42, 9),
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) VALUES (?, 'stripe', 'sub-1', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        assert asyncio.run(process_update(
            connection, _chat_member_update(101, 42, "member"), client, chat_id=-100
        )) == "processed"
        assert connection.execute("SELECT kind FROM outbox_jobs").fetchall()[0][0] == "site.restore"

        connection.execute(
            "UPDATE users SET telegram_membership_status = 'left', telegram_banned = 1, telegram_ban_source = 'admin' WHERE id = ?",
            (user["id"],),
        )
        assert asyncio.run(process_update(
            connection, _chat_member_update(102, 42, "member"), client, chat_id=-100
        )) == "processed"
        assert connection.execute("SELECT COUNT(*) FROM outbox_jobs WHERE kind = 'site.restore'").fetchone()[0] == 1


def test_chat_member_kicked_is_manual_and_duplicate_is_safe(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'membership-ban.db'}")
    db.init_schema()
    client = TelegramClient("test-token", transport=TelegramTransport())
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_user_id) VALUES (?, ?) RETURNING id", (42, 9)
        ).fetchone()
        update = _chat_member_update(103, 42, "kicked")
        assert asyncio.run(process_update(connection, update, client, chat_id=-100)) == "processed"
        assert asyncio.run(process_update(connection, update, client, chat_id=-100)) == "duplicate"
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert stored["telegram_banned"] == 1
        assert stored["telegram_ban_source"] == "admin"
        assert connection.execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0] == 1


def test_chat_member_system_kick_does_not_create_manual_ban_or_duplicate_job(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'membership-system-ban.db'}")
    db.init_schema()
    client = TelegramClient("test-token", transport=TelegramTransport())
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_user_id, telegram_ban_source) VALUES (?, ?, 'system') RETURNING id",
            (42, 9),
        ).fetchone()
        update = _chat_member_update(106, 42, "kicked")
        assert asyncio.run(process_update(connection, update, client, chat_id=-100)) == "processed"
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert stored["telegram_banned"] == 0
        assert stored["telegram_ban_source"] == "system"
        assert connection.execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0] == 0


def test_chat_member_ignores_unknown_or_wrong_chat(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'membership-ignore.db'}")
    db.init_schema()
    client = TelegramClient("test-token", transport=TelegramTransport())
    with db.connect() as connection:
        assert asyncio.run(process_update(
            connection, _chat_member_update(104, 999, "kicked"), client, chat_id=-100
        )) == "ignored"
        assert asyncio.run(process_update(
            connection, _chat_member_update(105, 999, "kicked", chat_id=-200), client, chat_id=-100
        )) == "ignored"
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_chat_member_event_is_accepted_for_special_channel(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'membership-channel.db'}")
    db.init_schema()
    client = TelegramClient("test-token", transport=TelegramTransport())
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id, wordpress_user_id) VALUES (?, ?) RETURNING id", (42, 9)
        ).fetchone()
        assert asyncio.run(process_update(
            connection,
            _chat_member_update(107, 42, "left", chat_id=-200),
            client,
            chat_id=("-100", "-200"),
        )) == "processed"
        stored = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert stored["telegram_membership_status"] == "left"


def test_join_request_is_approved_only_for_matching_active_invite(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'join-request.db'}")
    db.init_schema()
    transport = TelegramTransport()
    client = TelegramClient("test-token", transport=transport)
    with db.connect() as connection:
        user = connection.execute(
            "INSERT INTO users(telegram_id) VALUES (?) RETURNING id", (42,)
        ).fetchone()
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'stripe', 'sub-join', 'active', 'paid', '2999-01-01T00:00:00+00:00')",
            (user["id"],),
        )
        connection.execute(
            "INSERT INTO telegram_invites(user_id, chat_id, invite_link, expires_at) VALUES (?, '-100', ?, ?)",
            (user["id"], "https://t.me/+expected", (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()),
        )
        update = {
            "update_id": 108,
            "chat_join_request": {
                "chat": {"id": -100},
                "from": {"id": 42},
                "invite_link": {"invite_link": "https://t.me/+expected"},
            },
        }
        assert asyncio.run(process_update(connection, update, client, chat_id=("-100",))) == "approved"
        assert connection.execute("SELECT status FROM telegram_invites").fetchone()[0] == "used"
        assert transport.calls[0]["chat_id"] == -100
        assert transport.calls[0]["user_id"] == 42

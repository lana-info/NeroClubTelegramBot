# Nero Club Telegram Bot

Self-hosted backend for a paid Telegram community with WordPress access control and Google Sheets as an administration dashboard.

Для владельца проекта начните с [START-HERE.ru.md](START-HERE.ru.md): там
описано, как безопасно проверить систему на отдельном Telegram-чате, где брать
настройки и что не нужно делать самостоятельно.

## Current implementation

The first vertical slice contains:

- FastAPI backend;
- SQLite schema for users, subscriptions, inbox events, outbox jobs, Sheets commands and audit log;
- protected internal API for user records and Sheets commands;
- protected Dashboard preview and CSV export for the future Google Sheets panel;
- whitelist, manual allow/deny and manual access extension;
- Stripe webhook signature verification and duplicate-event protection;
- Stripe Checkout sessions with internal user metadata; confirmed `invoice.paid`
  events queue personal Telegram join-request invites;
- Stripe inbox/outbox worker for idempotent subscription-state updates;
- Telegram webhook with `/start`, `/status` and `/help`, including `update_id` deduplication;
- Telegram `chat_member` handling: ordinary leave queues WordPress deactivation,
  administrator ban is preserved until an explicit restore action;
- `/site-access` flow for active subscribers: permanent WordPress credentials are delivered privately and never stored;
- `/my-keys` flow for active subscribers: assigned application keys are delivered privately with their expiry date;
- persistent Telegram user menu and admin-only support inbox;
- Google Sheets actions `issue_credentials` and `resend_delivery` are queued without storing a password;
- protected `/internal/jobs/process-site-access` worker endpoint for queued site-access delivery;
- protected personal invite endpoint and dry-run Telegram membership reconciliation;
- Telegram join requests are approved only for the matching user and unexpired
  stored invite; personal invite links are valid for 24 hours and expired links
  are revoked by a scheduled worker;
- protected subscription reminder job for 7-day, 3-day and 1-day notifications;
- health endpoint;
- Docker configuration;
- tests for authentication, idempotent Sheets commands and Stripe webhook handling.

External Telegram, WordPress and Google credentials are intentionally not included. Telegram updates are accepted through a secret-protected webhook; the operational Google Sheets panel is connected through the Apps Script sync described below.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

After the backend receives a site-access command from the panel, run the worker with an authenticated request:

```bash
curl -X POST http://127.0.0.1:8000/internal/jobs/process-site-access \
  -H "Authorization: Bearer $ADMIN_API_TOKEN"
```

The worker requires configured Telegram and WordPress integrations. It never writes the generated password to the database, job payload, audit log or Google Sheets.

Telegram access operations are available to the admin API as `/internal/users/{user_id}/invite` and `/internal/jobs/reconcile-telegram`. Set `TELEGRAM_CHANNEL_ID` when the same subscription must also control a private channel. Reconciliation is non-destructive while `DRY_RUN=true`; only set it to `false` after testing the real chat, channel, and bot permissions.

The reminder endpoint is `/internal/jobs/send-reminders`. Run it once per day from the server scheduler. It is idempotent and sends only to users who enabled reminders in the bot. Use `REMINDERS_DRY_RUN=true` for reminder preview mode; it is separate from removal `DRY_RUN`. Set `PAYMENT_URL` when a payment page is available.

In sheet payment mode, `/pay` shows `NEW_MEMBER_PRICE_USD` to a user without
confirmed payment history and `RETURNING_MEMBER_PRICE_USD` to a returning user.
It also shows separate one-time and recurring payment links for each category.

The scheduler starts together with the backend using `docker compose up -d`. It processes Stripe, site-access, personal Telegram invites and expired invite revocation hourly, reminders daily, and Telegram reconciliation daily. The scheduler uses the same `ADMIN_API_TOKEN` and does not expose an additional port.

Application keys use `APP_KEYS_ENCRYPTION_KEY`. The protected admin API imports a key, while the bot only reveals it to the assigned active subscriber. The `Ключи приложений` tab keeps its separate sync in `KeysSync.gs`.

Set `ADMIN_TELEGRAM_IDS` to a comma-separated list of numeric Telegram IDs. Users can choose `💬 Связаться с администратором`; administrators receive the request and answer with `/reply REQUEST_ID TEXT`.

After configuring `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_URL`, call the protected
`/internal/telegram/setup-menu` endpoint once. It publishes the command list and
sets Telegram `allowed_updates` to `message`, `chat_member` and `chat_join_request`.

To connect the operational panel, copy `google-apps-script/SheetsSync.gs` and
`google-apps-script/KeysSync.gs` into Extensions → Apps Script for the new
spreadsheet. Set Script Properties `BACKEND_URL` and `ADMIN_API_TOKEN`. Run
`importCurrentSnapshot()` once before the first pull sync; it imports users and
subscription dates and replaces legacy `user_id` values with backend IDs. Then
run `syncAllSheets()` once and `installSheetsSyncTrigger()` once. Every five
minutes the panel first imports new or edited user rows, then executes commands
and refreshes users, site access, and Dashboard. If the backend has no users,
pull sync stops instead of erasing the sheet. Never store bot, payment,
WordPress, or encryption secrets in the sheet.

Tests:

```bash
pytest -q
```

For the complete scope and ordered implementation plan see [PRD.ru.md](PRD.ru.md) and [TASKS.ru.md](TASKS.ru.md).

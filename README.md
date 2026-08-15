# Nero Club Telegram Bot

Self-hosted backend for a paid Telegram community with WordPress access control and Google Sheets as an administration dashboard.

## Current implementation

The first vertical slice contains:

- FastAPI backend;
- SQLite schema for users, subscriptions, inbox events, outbox jobs, Sheets commands and audit log;
- protected internal API for user records and Sheets commands;
- protected Dashboard preview and CSV export for the future Google Sheets panel;
- whitelist, manual allow/deny and manual access extension;
- Stripe webhook signature verification and duplicate-event protection;
- Stripe inbox/outbox worker for idempotent subscription-state updates;
- Telegram webhook with `/start`, `/status` and `/help`, including `update_id` deduplication;
- `/site-access` flow for active subscribers: permanent WordPress credentials are delivered privately and never stored;
- Google Sheets actions `issue_credentials` and `resend_delivery` are queued without storing a password;
- protected `/internal/jobs/process-site-access` worker endpoint for queued site-access delivery;
- health endpoint;
- Docker configuration;
- tests for authentication, idempotent Sheets commands and Stripe webhook handling.

External Telegram, WordPress and Google credentials are intentionally not included. Telegram updates are accepted through a secret-protected webhook; the Dashboard can be previewed/exported locally, while direct Google Sheets provisioning remains a subsequent task.

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

Tests:

```bash
pytest -q
```

For the complete scope and ordered implementation plan see [PRD.ru.md](PRD.ru.md) and [TASKS.ru.md](TASKS.ru.md).

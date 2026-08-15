# Nero Club Telegram Bot

Self-hosted backend for a paid Telegram community with WordPress access control and Google Sheets as an administration dashboard.

## Current implementation

The first vertical slice contains:

- FastAPI backend;
- SQLite schema for users, subscriptions, inbox events, outbox jobs, Sheets commands and audit log;
- protected internal API for user records and Sheets commands;
- whitelist, manual allow/deny and manual access extension;
- Stripe webhook signature verification and duplicate-event protection;
- Telegram webhook with `/start`, `/status` and `/help`, including `update_id` deduplication;
- health endpoint;
- Docker configuration;
- tests for authentication, idempotent Sheets commands and Stripe webhook handling.

External Telegram, WordPress and Google credentials are intentionally not included. Telegram updates are accepted through a secret-protected webhook; payment workers, WordPress and Google Sheets provisioning remain subsequent tasks.

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

Tests:

```bash
pytest -q
```

For the complete scope and ordered implementation plan see [PRD.ru.md](PRD.ru.md) and [TASKS.ru.md](TASKS.ru.md).

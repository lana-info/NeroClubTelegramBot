# Telegram Membership Events Implementation Plan

> **For agentic workers:** Implement this plan task-by-task with a fresh test cycle after each task.

**Goal:** Synchronize known users' WordPress access with Telegram membership events while preserving a manual administrator ban.

**Architecture:** Telegram `chat_member` webhook updates are deduplicated in the existing inbox table. Membership state is stored on `users`; WordPress changes are queued in `outbox_jobs` and processed by the existing hourly worker. The daily reconciliation remains a safety net.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, asyncio, pytest, Telegram Bot API, existing WordPress HMAC plugin API.

## Global Constraints

- Unknown Telegram IDs are ignored.
- `DRY_RUN=true` must not call destructive Telegram or WordPress operations.
- A manual Telegram ban is stronger than whitelist or an active subscription.
- Duplicate Telegram updates and duplicate outbox operations must be idempotent.
- No secret, password, or webhook payload is added to Git.

### Task 1: Persist membership state and queue operations

**Files:**
- Modify: `app/db.py`
- Modify: `app/access.py`
- Modify: `app/site_access.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Add `users.telegram_membership_status` with default `unknown`.
- Add `users.telegram_banned` with default `0`.
- Add `users.telegram_ban_source` with values `admin` or `system` when present.
- Add `queue_site_access_job(db, user_id, action, aggregate_key)` for `deactivate` and `restore`.
- Extend the site worker to process `site.deactivate` and `site.restore` through WordPress actions `deactivate` and `restore`.

- [x] Add schema creation and additive migrations for all three columns.
- [x] Make `effective_access()` return `denied` when `telegram_banned = 1`.
- [x] Add idempotent queue insertion using the existing unique `(kind, aggregate_key)` constraint.
- [x] Ensure the WordPress worker marks successful access jobs done and failed jobs retryable without exposing secrets.
- [x] Add tests for both WordPress actions, duplicate queue keys, and manual-ban access denial.

### Task 2: Process Telegram `chat_member` updates

**Files:**
- Modify: `app/telegram_updates.py`
- Modify: `app/main.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- `process_update()` accepts `chat_member` updates in addition to message updates.
- A matching chat-member event contains `chat.id`, `new_chat_member.user.id`, and `new_chat_member.status`.

- [x] Ignore events for a configured chat ID that does not match.
- [x] Find users by Telegram ID and ignore unknown users.
- [x] For `left`, store `telegram_membership_status = 'left'` and queue WordPress deactivation.
- [x] For `kicked`, store `telegram_membership_status = 'kicked'`, `telegram_banned = 1`, `telegram_ban_source = 'admin'`, and queue deactivation unless the system already marked the ban.
- [x] For `member`, `restricted`, `administrator`, or `creator`, store the status.
- [x] On a return from `left`, queue WordPress restore only when the user is not manually banned and `effective_access` is active.
- [x] Keep update deduplication before any side effect.
- [x] Add tests for left, admin kicked, system kicked, return, wrong chat, unknown user, and duplicate update.

### Task 3: Preserve manual-ban semantics in reconciliation and Sheets

**Files:**
- Modify: `app/membership.py`
- Modify: `app/access.py`
- Modify: `README.md`
- Modify: `START-HERE.ru.md`
- Test: `tests/test_app.py`

- [x] Before an automatic expiry ban, mark the membership ban source as `system` so the webhook does not convert it into a manual ban.
- [x] When daily reconciliation sees an active known user in `left`, queue deactivation.
- [x] Add the Sheets action `restore_telegram` to clear only the manual Telegram ban and queue restore when access is active.
- [x] Keep `deny` as an independent administrative access override.
- [x] Document that an administrator ban requires an explicit restore action.
- [x] Add tests for system ban, manual restore, and whitelist not bypassing a manual ban.

### Task 4: Configure webhook update types and documentation

**Files:**
- Modify: `app/integrations/telegram.py`
- Modify: `app/main.py`
- Modify: `docs/superpowers/specs/2026-08-16-telegram-membership-events-design.md`
- Modify: `DEPLOYMENT.ru.md`

- [x] Add a Telegram `setWebhook` client method with `allowed_updates` including `message` and `chat_member`.
- [x] Add a protected internal setup endpoint or update the existing setup command so production setup publishes these update types.
- [x] Document the one-time setup step and the meaning of `DRY_RUN`.
- [x] Avoid opening another port or adding an external service.

### Task 5: Full verification and delivery

**Files:**
- No new runtime files.

- [x] Run `./.venv/bin/pytest -q`.
- [x] Run `./.venv/bin/python -m compileall -q app`.
- [x] Run `git diff --check`.
- [ ] Inspect the complete diff for secrets and unrelated changes.
- [ ] Commit and push the implementation to `agent/initial-project`.

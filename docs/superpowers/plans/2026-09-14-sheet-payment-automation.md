# Sheet Payment Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a confirmed Google Sheets payment extend an existing member by one calendar month exactly once and expose all access state as backend-owned projections.

**Architecture:** The backend accepts `payment_id`, Telegram ID and payment date, records an immutable `sheet` inbox event plus a result row, then updates the member's effective paid-until date. Apps Script assigns missing UUIDs, submits new rows and redraws the payment and access views; it no longer imports computed access data every five minutes.

**Tech Stack:** Python 3.11, FastAPI, SQLite, pytest, Google Apps Script.

**Spec:** `docs/superpowers/specs/2026-09-14-sheet-payment-automation-design.md`

## Global Constraints

- A monthly payment means exactly one calendar month, with the last valid day used when the next month is shorter.
- Duplicate `payment_id` must never extend access a second time.
- `telegram_id` must already belong to a user; a failed row must not mutate access.
- Do not put tokens or payment secrets into Sheets, code, tests or logs.
- Preserve existing manual whitelist and emergency-command behavior.

---

### Task 1: Persist and calculate sheet payments

**Files:**
- Modify: `app/db.py`
- Modify: `app/sheets.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Produces `SHEET_PAYMENT_HEADERS` and `process_sheet_payments(db, rows) -> list[dict[str, Any]]`.
- Produces `rows_for_payments_sheet(db) -> list[list[Any]]`.
- Consumes user identity from `users.telegram_id`, subscriptions and the existing `outbox_jobs` invite queue.

- [ ] **Step 1: Write failing tests for calendar arithmetic and idempotency**

```python
def test_sheet_payment_extends_from_later_of_paid_date_and_current_expiry(tmp_path):
    db = database(tmp_path)
    with db.connect() as connection:
        user = upsert_user(connection, {"telegram_id": 42})
        connection.execute(
            "INSERT INTO subscriptions(user_id, provider, provider_subscription_id, billing_status, payment_status, provider_paid_until) "
            "VALUES (?, 'sheet', 'manual-42', 'active', 'paid', '2026-01-31T00:00:00+00:00')", (user["id"],)
        )
        first = process_sheet_payments(connection, [{"payment_id": "p-1", "telegram_id": 42, "paid_at": "2026-01-15"}])
        second = process_sheet_payments(connection, [{"payment_id": "p-1", "telegram_id": 42, "paid_at": "2026-01-15"}])
    assert first[0]["applied_until"] == "2026-02-28"
    assert second[0]["status"] == "duplicate"
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `pytest tests/test_app.py -k sheet_payment -v`

Expected: FAIL because `process_sheet_payments` is not defined.

- [ ] **Step 3: Add result storage and domain helpers**

Create `sheet_payment_results` in `Database.init_schema()` with primary key `payment_id`, a foreign key to `users`, `paid_at`, `applied_until`, `status`, `error`, and timestamp. In `app/sheets.py`, parse ISO date-only values at UTC midnight, implement an `add_calendar_month()` helper with `calendar.monthrange`, record the incoming event through `inbox_events(provider='sheet')`, and store the resulting response in `sheet_payment_results`.

For an accepted row, select the latest valid paid-until date for that user, calculate `max(existing_until, paid_at) + one month`, upsert the user’s `sheet/manual-<telegram_id>` subscription, and insert one `telegram.invite` job keyed by payment ID. For a duplicate payment ID, return its stored result without changing subscriptions or jobs. For blank, malformed or unknown Telegram IDs, persist/return `status='error'` and do not create a subscription.

- [ ] **Step 4: Add the backend-owned payment projection**

Define the exact header order:

```python
SHEET_PAYMENT_HEADERS = [
    "payment_id", "telegram_id", "paid_at", "plan", "status",
    "applied_until", "processed_at", "error",
]
```

Return rows in deterministic processing order from `sheet_payment_results`; set `plan` to `monthly`. Add tests covering an unknown ID, a date from the 31st to February, and the projected headers and result row.

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/test_app.py -k 'sheet_payment or sheet_snapshot' -v`

Expected: PASS.

### Task 2: Expose controlled Sheets endpoints and preserve effective access

**Files:**
- Modify: `app/main.py`
- Modify: `app/sheets.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Produces `POST /internal/sheets/payments`, `GET /internal/sheets/payments`, and `POST /internal/sheets/whitelist`.
- Consumes the functions from Task 1.
- Produces backend-only user rows whose dates and access cannot be overwritten by periodic Sheets sync.

- [ ] **Step 1: Write failing endpoint and whitelist tests**

```python
def test_sheet_payment_endpoint_rejects_unknown_member(client):
    response = client.post(
        "/internal/sheets/payments", headers={"Authorization": "Bearer test-token"},
        json={"payments": [{"payment_id": "p-missing", "telegram_id": 999, "paid_at": "2026-01-01"}]},
    )
    assert response.status_code == 200
    assert response.json()["payments"][0]["status"] == "error"
```

```python
def test_sheet_whitelist_update_changes_only_the_whitelist_flag(tmp_path):
    # create a user with a paid subscription, sync whitelist=yes, then assert
    # provider_paid_until and subscription status are unchanged.
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/test_app.py -k 'payment_endpoint or whitelist_update' -v`

Expected: FAIL because the endpoints do not exist.

- [ ] **Step 3: Add bounded endpoints**

Implement `POST /internal/sheets/payments` accepting at most 500 rows under a `payments` key and returning the per-row results from `process_sheet_payments`. Implement `GET /internal/sheets/payments` with the exact headers and rows from `rows_for_payments_sheet`.

Implement `POST /internal/sheets/whitelist` accepting at most 1,000 records of `{user_id, whitelist}`. Resolve each numeric internal user ID, update only `users.whitelist`, append an audit-log event only when the value changes, and return a count. Return HTTP 422 for invalid shapes and 404 for an unknown user ID.

- [ ] **Step 4: Stop using repeated snapshot import as live state**

Retain `/internal/sheets/import` and `importCurrentSnapshot()` strictly for the documented one-time legacy migration. Do not route normal payments or calculated access through it. Update tests to assert a payment-generated date remains unchanged after a whitelist update and after rendering user/site rows.

- [ ] **Step 5: Run backend tests**

Run: `pytest tests/test_app.py -v`

Expected: PASS.

### Task 3: Replace Apps Script’s live import with payment and whitelist sync

**Files:**
- Modify: `google-apps-script/SheetsSync.gs`
- Modify: `START-HERE.ru.md`

**Interfaces:**
- Consumes `/internal/sheets/payments`, `/internal/sheets/whitelist`, and `/internal/sheets/payments` GET from Task 2.
- Produces a sheet layout with only `telegram_id` and `paid_at` as regular payment inputs.

- [ ] **Step 1: Add payment tab constants and helpers**

Add `PAYMENTS_SHEET = 'Платежи'` and helpers that: ensure the eight payment headers in their exact order; create a UUID with `Utilities.getUuid()` for every non-empty new payment row lacking `payment_id`; post only `payment_id`, `telegram_id` and `paid_at`; and write backend payment rows back using `writeBackendRows_`.

- [ ] **Step 2: Replace `importCurrentSnapshot()` inside `syncAllSheets()`**

Keep `importCurrentSnapshot()` callable for the one-time migration but remove it from `syncAllSheets()`. Add `syncWhitelists_()` that reads only `user_id` and `whitelist` from `Пользователи`, posts them to the new endpoint, then redraws the users sheet. This prevents stale calculated dates, provider values and access statuses from flowing from Sheets into the backend.

- [ ] **Step 3: Sync payment rows before all views are redrawn**

Call `syncPayments_()` and `syncWhitelists_()` before commands and rendering. Render `Платежи` via `writeBackendRows_(PAYMENTS_SHEET, '/internal/sheets/payments')`. Keep `Доступ к сайту`, Dashboard and Settings as backend projections.

- [ ] **Step 4: Document safe deployment**

Update `START-HERE.ru.md` with these precise operator steps: make a Drive copy; rename the existing `Платежи` tab to `Платежи (архив)`; paste the updated Apps Script; run `importCurrentSnapshot()` exactly once; run `syncAllSheets()`; install the five-minute trigger; enter future payments only as Telegram ID and payment date. State that old payment history is archived and never replayed as new months.

- [ ] **Step 5: Manually validate the script contract**

Check that the emitted Apps Script names match the backend endpoints and exact column headers. Run `git diff --check`.

### Task 4: Full verification and release handoff

**Files:**
- Modify: `TASKS.ru.md`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes all previous tasks.
- Produces a verified implementation with a documented, reversible deployment sequence.

- [ ] **Step 1: Run the complete automated suite**

Run: `pytest -q`

Expected: PASS.

- [ ] **Step 2: Review the final change set**

Run: `git diff --check && git status --short && git diff -- app/db.py app/sheets.py app/main.py google-apps-script/SheetsSync.gs START-HERE.ru.md tests/test_app.py TASKS.ru.md`

Expected: only payment automation, its tests and project-status documentation appear.

- [ ] **Step 3: Record the project status**

Update the existing Google Sheets task in `TASKS.ru.md` to note that manual payment events are idempotent, one-time snapshot import is separate from live sync, and production still requires the documented test-sheet check.

- [ ] **Step 4: Hand off without committing or deploying**

Report changed files, successful tests, the archived-history constraint, and the three required operator actions. Do not commit, push, expose `ADMIN_API_TOKEN`, or run Apps Script against the live spreadsheet without the owner’s explicit approval.

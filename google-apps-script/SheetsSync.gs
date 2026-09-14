/**
 * Nero Club — synchronizes the operational Google Sheets panel with the backend.
 *
 * Safe order for a new installation:
 * 1. Set BACKEND_URL and ADMIN_API_TOKEN in Script Properties.
 * 2. Run importCurrentSnapshot() once.
 * 3. Run syncAllSheets() once, then installSheetsSyncTrigger().
 *
 * The old spreadsheet is never touched by this script.
 */
const SHEETS_BACKEND_URL_PROPERTY = 'BACKEND_URL';
const SHEETS_ADMIN_TOKEN_PROPERTY = 'ADMIN_API_TOKEN';
const USERS_SHEET = 'Пользователи';
const PAYMENTS_SHEET = 'Платежи';
const PAYMENT_CORRECTIONS_SHEET = 'Исправления оплат';
const SITE_SHEET = 'Доступ к сайту';
const DASHBOARD_SHEET = 'Dashboard';
const SETTINGS_SHEET = 'Настройки';
const PAYMENT_HEADERS = [
  'payment_id', 'telegram_id', 'paid_at', 'plan', 'status', 'applied_until', 'processed_at', 'error',
  'payment_provider', 'amount_usd', 'provider_payment_id'
];
const LEGACY_PAYMENT_HEADERS = [
  'payment_id', 'telegram_id', 'paid_at', 'plan', 'status', 'applied_until', 'processed_at', 'error'
];
const PAYMENT_CORRECTION_HEADERS = [
  'correction_id', 'payment_id', 'telegram_id', 'before', 'after', 'note', 'corrected_at'
];

function installSheetsSyncTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'syncAllSheets') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('syncAllSheets').forSpreadsheet(SpreadsheetApp.getActive()).onEdit().create();
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Nero Club')
    .addItem('Открыть панель', 'showNeroClubSidebar')
    .addToUi();
}

function showNeroClubSidebar() {
  ensurePaymentsSheet_();
  ensurePaymentCorrectionsSheet_();
  SpreadsheetApp.getUi().showSidebar(
    HtmlService.createHtmlOutput(NERO_CLUB_PANEL_HTML).setTitle('Nero Club')
  );
}

function backendRequest_(path, method, payload) {
  const properties = PropertiesService.getScriptProperties();
  const backendUrl = properties.getProperty(SHEETS_BACKEND_URL_PROPERTY);
  const adminToken = properties.getProperty(SHEETS_ADMIN_TOKEN_PROPERTY);
  if (!backendUrl || !adminToken) throw new Error('BACKEND_URL and ADMIN_API_TOKEN are required');
  const options = { method: method || 'get', headers: { Authorization: 'Bearer ' + adminToken }, muteHttpExceptions: true };
  if (payload !== undefined) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(payload);
  }
  const response = UrlFetchApp.fetch(backendUrl.replace(/\/$/, '') + path, options);
  const body = JSON.parse(response.getContentText() || '{}');
  if (response.getResponseCode() >= 400) throw new Error(body.detail || 'Backend request failed');
  return body;
}

function headerIndex_(headers) {
  const result = {};
  headers.forEach(function(header, column) { result[header] = column; });
  return result;
}

function importCurrentSnapshot() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(USERS_SHEET);
  if (!sheet) throw new Error('Sheet not found: ' + USERS_SHEET);
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) throw new Error('Users sheet is empty');
  const index = headerIndex_(values[0]);
  ['user_id', 'telegram_id', 'username', 'wordpress_email', 'wordpress_role', 'provider',
    'provider_paid_until', 'manual_access_until', 'whitelist', 'access_override'].forEach(function(header) {
      if (index[header] === undefined) throw new Error('Missing header: ' + header);
    });
  const rows = [];
  const sourceRows = [];
  for (let row = 1; row < values.length; row++) {
    const value = values[row];
    if (!value[index.telegram_id]) continue;
    rows.push({
      telegram_id: Number(value[index.telegram_id]), username: value[index.username] || '',
      wordpress_email: value[index.wordpress_email] || '', wordpress_role: value[index.wordpress_role] || '',
      provider: value[index.provider] || '', provider_paid_until: value[index.provider_paid_until] || null,
      manual_access_until: value[index.manual_access_until] || null, whitelist: value[index.whitelist] || 'no',
      access_override: value[index.access_override] || 'none'
    });
    sourceRows.push(row + 1);
  }
  const result = backendRequest_('/internal/sheets/import', 'post', { users: rows });
  const ids = {};
  (result.users || []).forEach(function(item) { ids[String(item.telegram_id)] = item.user_id; });
  sourceRows.forEach(function(sourceRow) {
    const telegramId = values[sourceRow - 1][index.telegram_id];
    if (ids[String(telegramId)]) sheet.getRange(sourceRow, index.user_id + 1).setValue(ids[String(telegramId)]);
  });
  return result;
}

function syncSheetCommands_() {
  syncCommandsFromSheet_(USERS_SHEET);
  syncCommandsFromSheet_(SITE_SHEET);
}

function ensurePaymentsSheet_() {
  const spreadsheet = SpreadsheetApp.getActive();
  let sheet = spreadsheet.getSheetByName(PAYMENTS_SHEET);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(PAYMENTS_SHEET);
    sheet.getRange(1, 1, 1, PAYMENT_HEADERS.length).setValues([PAYMENT_HEADERS]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, PAYMENT_HEADERS.length).setFontWeight('bold');
    sheet.autoResizeColumns(1, PAYMENT_HEADERS.length);
    return sheet;
  }
  const current = sheet.getRange(1, 1, 1, PAYMENT_HEADERS.length).getDisplayValues()[0];
  if (current.slice(0, LEGACY_PAYMENT_HEADERS.length).join('|') === LEGACY_PAYMENT_HEADERS.join('|') &&
      current.slice(LEGACY_PAYMENT_HEADERS.length).every(function(value) { return !value; })) {
    sheet.getRange(1, 1, 1, PAYMENT_HEADERS.length).setValues([PAYMENT_HEADERS]);
    sheet.getRange(1, 1, 1, PAYMENT_HEADERS.length).setFontWeight('bold');
    sheet.autoResizeColumns(1, PAYMENT_HEADERS.length);
    return sheet;
  }
  if (current.join('|') !== PAYMENT_HEADERS.join('|')) {
    throw new Error('Rename the existing payment history tab to "Платежи (архив)" before enabling payment sync');
  }
  return sheet;
}

function ensurePaymentCorrectionsSheet_() {
  const spreadsheet = SpreadsheetApp.getActive();
  let sheet = spreadsheet.getSheetByName(PAYMENT_CORRECTIONS_SHEET);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(PAYMENT_CORRECTIONS_SHEET);
    sheet.getRange(1, 1, 1, PAYMENT_CORRECTION_HEADERS.length).setValues([PAYMENT_CORRECTION_HEADERS]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, PAYMENT_CORRECTION_HEADERS.length).setFontWeight('bold');
    sheet.autoResizeColumns(1, PAYMENT_CORRECTION_HEADERS.length);
  }
  return sheet;
}

function syncPayments_() {
  const sheet = ensurePaymentsSheet_();
  const values = sheet.getDataRange().getValues();
  const headers = values[0].map(function(value) { return String(value); });
  const index = headerIndex_(headers);
  const payments = [];
  for (let row = 1; row < values.length; row++) {
    const value = values[row];
    const telegramId = value[index.telegram_id];
    const paidAt = sheetDate_(value[index.paid_at]);
    if (!telegramId && !paidAt) continue;
    let paymentId = value[index.payment_id];
    if (!paymentId) {
      paymentId = Utilities.getUuid();
      sheet.getRange(row + 1, index.payment_id + 1).setValue(paymentId);
    }
    payments.push({
      payment_id: paymentId, telegram_id: telegramId, paid_at: paidAt,
      payment_provider: value[index.payment_provider], amount_usd: value[index.amount_usd],
      provider_payment_id: value[index.provider_payment_id]
    });
  }
  if (payments.length) backendRequest_('/internal/sheets/payments', 'post', {payments: payments});
}

function sheetDate_(value) {
  if (value instanceof Date && !isNaN(value.getTime())) {
    return Utilities.formatDate(value, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }
  return String(value || '').trim();
}

function panelTelegramId_(value) {
  const telegramId = String(value || '').trim();
  if (!/^\d+$/.test(telegramId)) throw new Error('Введите числовой Telegram ID');
  return telegramId;
}

function panelPaymentDate_(value) {
  const paidAt = String(value || '').trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(paidAt)) throw new Error('Выберите дату оплаты');
  return paidAt;
}

function refreshPanelViews_() {
  ensurePaymentCorrectionsSheet_();
  writeBackendRows_(USERS_SHEET, '/internal/sheets/users');
  writeBackendRows_(PAYMENTS_SHEET, '/internal/sheets/payments');
  writeBackendRows_(SITE_SHEET, '/internal/sheets/site-access');
  writeBackendRows_(DASHBOARD_SHEET, '/internal/sheets/dashboard');
  writeBackendRows_(PAYMENT_CORRECTIONS_SHEET, '/internal/sheets/payment-corrections');
  if (SpreadsheetApp.getActive().getSheetByName(SETTINGS_SHEET)) {
    writeBackendRows_(SETTINGS_SHEET, '/internal/sheets/settings');
  }
}

function lookupPanelUser(form) {
  const telegramId = panelTelegramId_(form.telegram_id);
  const users = backendRequest_('/internal/sheets/users', 'get');
  const index = headerIndex_(users.headers || []);
  const row = (users.rows || []).find(function(item) { return String(item[index.telegram_id]) === telegramId; });
  if (!row) return {ok: false, message: 'Пользователь с таким Telegram ID не найден'};
  const name = row[index.username] || 'Имя не указано';
  const email = row[index.wordpress_email] || 'e-mail не указан';
  const until = String(row[index.provider_paid_until] || '').slice(0, 10) || 'нет активной даты';
  return {ok: true, message: name + ' · ' + email + ' · доступ до ' + until};
}

function lookupPanelPayments(form) {
  const telegramId = panelTelegramId_(form.telegram_id);
  const body = backendRequest_('/internal/sheets/payments', 'get');
  const index = headerIndex_(body.headers || []);
  const payments = (body.rows || []).filter(function(row) {
    return String(row[index.telegram_id]) === telegramId && row[index.status] === 'processed';
  }).map(function(row) {
    return {
      payment_id: row[index.payment_id], paid_at: String(row[index.paid_at] || '').slice(0, 10),
      payment_provider: row[index.payment_provider] || 'stripe', amount_usd: row[index.amount_usd] || 10,
      provider_payment_id: row[index.provider_payment_id] || ''
    };
  });
  return {payments: payments};
}

function submitPanelPayment(form) {
  const telegramId = panelTelegramId_(form.telegram_id);
  const paidAt = panelPaymentDate_(form.paid_at);
  const paymentId = Utilities.getUuid();
  const response = backendRequest_('/internal/sheets/payments', 'post', {
    payments: [{
      payment_id: paymentId, telegram_id: telegramId, paid_at: paidAt,
      payment_provider: String(form.payment_provider || '').toLowerCase(),
      amount_usd: Number(form.amount_usd), provider_payment_id: String(form.provider_payment_id || '').trim()
    }]
  });
  refreshPanelViews_();
  const result = (response.payments || [])[0] || {};
  if (result.status !== 'processed') {
    return {ok: false, message: result.error || 'Платёж не обработан. Проверьте Telegram ID.'};
  }
  return {ok: true, message: 'Готово. Доступ продлён до ' + result.applied_until + '.'};
}

function submitPanelPaymentCorrection(form) {
  const response = backendRequest_('/internal/sheets/payment-corrections', 'post', {
    payment_id: String(form.payment_id || '').trim(), telegram_id: panelTelegramId_(form.telegram_id), paid_at: panelPaymentDate_(form.paid_at),
    payment_provider: String(form.payment_provider || '').toLowerCase(), amount_usd: Number(form.amount_usd),
    provider_payment_id: String(form.provider_payment_id || '').trim(), note: String(form.note || '').trim()
  });
  refreshPanelViews_();
  return {ok: true, message: 'Исправлено. Новый срок: ' + response.payment.applied_until + '.'};
}

function submitPanelWhitelist(form) {
  const telegramId = panelTelegramId_(form.telegram_id);
  const enabled = form.enabled === true || form.enabled === 'true';
  const users = backendRequest_('/internal/sheets/users', 'get');
  const headers = users.headers || [];
  const index = headerIndex_(headers);
  const row = (users.rows || []).find(function(item) {
    return String(item[index.telegram_id]) === telegramId;
  });
  if (!row || index.user_id === undefined) throw new Error('Пользователь с таким Telegram ID не найден');
  backendRequest_('/internal/sheets/whitelist', 'post', {
    users: [{user_id: Number(row[index.user_id]), whitelist: enabled ? 'yes' : 'no'}]
  });
  refreshPanelViews_();
  return {ok: true, message: enabled ? 'Пользователь добавлен в whitelist.' : 'Пользователь удалён из whitelist.'};
}

function syncWhitelists_() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(USERS_SHEET);
  if (!sheet) throw new Error('Sheet not found: ' + USERS_SHEET);
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;
  const index = headerIndex_(values[0]);
  ['user_id', 'whitelist'].forEach(function(header) {
    if (index[header] === undefined) throw new Error('Missing header in ' + USERS_SHEET + ': ' + header);
  });
  const users = [];
  for (let row = 1; row < values.length; row++) {
    const value = values[row];
    if (value[index.user_id]) users.push({user_id: Number(value[index.user_id]), whitelist: value[index.whitelist]});
  }
  if (users.length) backendRequest_('/internal/sheets/whitelist', 'post', {users: users});
}

function syncSettings_() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(SETTINGS_SHEET);
  if (!sheet) return;
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;
  const index = headerIndex_(values[0]);
  ['name', 'enabled'].forEach(function(header) {
    if (index[header] === undefined) throw new Error('Missing header in ' + SETTINGS_SHEET + ': ' + header);
  });
  const flags = [];
  for (let row = 1; row < values.length; row++) {
    if (values[row][index.name]) flags.push({name: values[row][index.name], enabled: values[row][index.enabled]});
  }
  backendRequest_('/internal/sheets/settings', 'post', {flags: flags});
}

function syncCommandsFromSheet_(sheetName) {
  const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!sheet) return;
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;
  const index = headerIndex_(values[0]);
  ['action', 'command_id', 'user_id', 'last_result'].forEach(function(header) {
    if (index[header] === undefined) throw new Error('Missing header in ' + sheetName + ': ' + header);
  });
  for (let row = 1; row < values.length; row++) {
    const value = values[row];
    const action = String(value[index.action] || 'none').toLowerCase();
    if (!action || action === 'none') continue;
    let commandId = value[index.command_id];
    if (!commandId) {
      commandId = 'sheets-' + sheetName + '-' + (row + 1) + '-' + new Date().getTime();
      sheet.getRange(row + 1, index.command_id + 1).setValue(commandId);
    }
    const payload = { command_id: commandId, user_id: Number(value[index.user_id]), action: action };
    if (index.manual_access_until !== undefined) payload.manual_access_until = value[index.manual_access_until] || null;
    try {
      const result = backendRequest_('/internal/sheets/commands', 'post', payload);
      sheet.getRange(row + 1, index.last_result + 1).setValue(result.result || result.status || 'done');
    } catch (error) {
      sheet.getRange(row + 1, index.last_result + 1).setValue('ERROR: ' + error.message);
    }
  }
}

function writeBackendRows_(sheetName, endpoint) {
  const body = backendRequest_(endpoint, 'get');
  if (!body.headers || !body.rows) throw new Error('Backend returned invalid rows for ' + sheetName);
  const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!sheet) throw new Error('Sheet not found: ' + sheetName);
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  const responseIndex = headerIndex_(body.headers);
  const output = body.rows.map(function(row) {
    return headers.map(function(header) { return responseIndex[header] === undefined ? '' : row[responseIndex[header]]; });
  });
  sheet.getRange(2, 1, Math.max(sheet.getMaxRows() - 1, output.length), headers.length).clearContent();
  if (output.length) sheet.getRange(2, 1, output.length, headers.length).setValues(output);
}

function syncAllSheets() {
  ensureLicensesSheet();
  ensurePaymentsSheet_();
  ensurePaymentCorrectionsSheet_();
  syncPayments_();
  syncWhitelists_();
  syncSheetCommands_();
  syncLicenses();
  syncSettings_();
  writeBackendRows_(USERS_SHEET, '/internal/sheets/users');
  writeBackendRows_(PAYMENTS_SHEET, '/internal/sheets/payments');
  writeBackendRows_(SITE_SHEET, '/internal/sheets/site-access');
  writeBackendRows_(DASHBOARD_SHEET, '/internal/sheets/dashboard');
  writeBackendRows_(PAYMENT_CORRECTIONS_SHEET, '/internal/sheets/payment-corrections');
  if (SpreadsheetApp.getActive().getSheetByName(SETTINGS_SHEET)) {
    writeBackendRows_(SETTINGS_SHEET, '/internal/sheets/settings');
  }
}

const NERO_CLUB_PANEL_HTML = `<!doctype html>
<html><head><base target="_top"><style>
body { font: 14px Arial, sans-serif; color: #202124; margin: 18px; }
h2 { margin: 0 0 16px; font-size: 20px; }
h3 { margin: 22px 0 8px; font-size: 15px; }
label { display: block; margin: 10px 0 5px; font-weight: 600; }
input, select { box-sizing: border-box; width: 100%; padding: 9px; border: 1px solid #dadce0; border-radius: 5px; }
button { width: 100%; margin-top: 14px; padding: 10px; color: #fff; background: #1a73e8; border: 0; border-radius: 5px; cursor: pointer; font-weight: 600; }
button.secondary { background: #5f6368; }
.message { display: none; margin-top: 12px; padding: 10px; border-radius: 5px; line-height: 1.35; }
.ok { display: block; background: #e6f4ea; color: #137333; }
.error { display: block; background: #fce8e6; color: #c5221f; }
.member { margin: 8px 0; color: #137333; line-height: 1.35; }
</style></head><body>
<h2>Nero Club</h2>
<form id="payment-form">
  <h3>Добавить платёж</h3>
  <label for="payment-id">Telegram ID</label>
  <input id="payment-id" required inputmode="numeric" autocomplete="off">
  <div id="payment-member" class="member"></div>
  <label for="paid-at">Дата оплаты</label>
  <input id="paid-at" type="date" required>
  <label for="payment-provider">Способ оплаты</label>
  <select id="payment-provider"><option value="stripe">Stripe</option><option value="paypal">PayPal</option></select>
  <label for="amount-usd">Сумма</label>
  <select id="amount-usd"><option value="10">$10</option><option value="20">$20</option></select>
  <label for="provider-payment-id">ID платежа (необязательно)</label>
  <input id="provider-payment-id" autocomplete="off">
  <button id="payment-button" type="submit">Продлить подписку</button>
  <div id="payment-message" class="message"></div>
</form>
<form id="correction-form">
  <h3>Исправить проведённый платёж</h3>
  <label for="correction-id">Telegram ID</label>
  <input id="correction-id" required inputmode="numeric" autocomplete="off">
  <div id="correction-member" class="member"></div>
  <label for="correction-payment-id">Платёж для исправления</label>
  <select id="correction-payment-id" required><option value="">Сначала укажите Telegram ID</option></select>
  <label for="correction-date">Исправленная дата оплаты</label>
  <input id="correction-date" type="date" required>
  <label for="correction-provider">Способ оплаты</label>
  <select id="correction-provider"><option value="stripe">Stripe</option><option value="paypal">PayPal</option></select>
  <label for="correction-amount">Сумма</label>
  <select id="correction-amount"><option value="10">$10</option><option value="20">$20</option></select>
  <label for="correction-provider-id">ID платежа Stripe/PayPal</label>
  <input id="correction-provider-id" autocomplete="off">
  <label for="correction-note">Что исправлено и источник</label>
  <input id="correction-note" required placeholder="Например: Источник PayPal, дата 23 июля исправлена на 23 августа">
  <button id="correction-button" type="submit">Сохранить исправление</button>
  <div id="correction-message" class="message"></div>
</form>
<form id="whitelist-form">
  <h3>Whitelist</h3>
  <label for="whitelist-id">Telegram ID</label>
  <input id="whitelist-id" required inputmode="numeric" autocomplete="off">
  <button id="whitelist-add" type="button">Добавить в whitelist</button>
  <button id="whitelist-remove" class="secondary" type="button">Убрать из whitelist</button>
  <div id="whitelist-message" class="message"></div>
</form>
<script>
document.getElementById('paid-at').value = new Date().toISOString().slice(0, 10);
document.getElementById('correction-date').value = new Date().toISOString().slice(0, 10);
function showMessage(id, result) {
  const node = document.getElementById(id);
  node.textContent = result.message;
  node.className = 'message ' + (result.ok ? 'ok' : 'error');
}
function request(button, messageId, method, payload) {
  button.disabled = true;
  google.script.run
    .withSuccessHandler(function(result) { button.disabled = false; showMessage(messageId, result); })
    .withFailureHandler(function(error) { button.disabled = false; showMessage(messageId, {ok: false, message: error.message || 'Не удалось выполнить действие'}); })
    [method](payload);
}
function lookup(inputId, resultId) {
  const value = document.getElementById(inputId).value;
  if (!value) return;
  google.script.run.withSuccessHandler(function(result) {
    const node = document.getElementById(resultId);
    node.textContent = result.message;
    node.style.color = result.ok ? '#137333' : '#c5221f';
  }).withFailureHandler(function() {}).lookupPanelUser({telegram_id: value});
}
document.getElementById('payment-id').addEventListener('change', function() { lookup('payment-id', 'payment-member'); });
function loadCorrectionPayments() {
  const telegramId = document.getElementById('correction-id').value;
  if (!telegramId) return;
  google.script.run.withSuccessHandler(function(result) {
    const select = document.getElementById('correction-payment-id');
    select.innerHTML = '<option value="">Выберите платёж</option>';
    (result.payments || []).forEach(function(payment) {
      const option = document.createElement('option');
      option.value = payment.payment_id;
      option.textContent = payment.paid_at + ' · ' + payment.payment_provider + ' · $' + payment.amount_usd;
      option.dataset.payment = JSON.stringify(payment);
      select.appendChild(option);
    });
  }).lookupPanelPayments({telegram_id: telegramId});
}
document.getElementById('correction-id').addEventListener('change', function() {
  lookup('correction-id', 'correction-member');
  loadCorrectionPayments();
});
document.getElementById('correction-payment-id').addEventListener('change', function() {
  const option = this.options[this.selectedIndex];
  if (!option.dataset.payment) return;
  const payment = JSON.parse(option.dataset.payment);
  document.getElementById('correction-date').value = payment.paid_at;
  document.getElementById('correction-provider').value = payment.payment_provider;
  document.getElementById('correction-amount').value = payment.amount_usd;
  document.getElementById('correction-provider-id').value = payment.provider_payment_id;
});
document.getElementById('payment-form').addEventListener('submit', function(event) {
  event.preventDefault();
  request(document.getElementById('payment-button'), 'payment-message', 'submitPanelPayment', {
    telegram_id: document.getElementById('payment-id').value,
    paid_at: document.getElementById('paid-at').value,
    payment_provider: document.getElementById('payment-provider').value,
    amount_usd: document.getElementById('amount-usd').value,
    provider_payment_id: document.getElementById('provider-payment-id').value
  });
});
document.getElementById('correction-form').addEventListener('submit', function(event) {
  event.preventDefault();
  request(document.getElementById('correction-button'), 'correction-message', 'submitPanelPaymentCorrection', {
    payment_id: document.getElementById('correction-payment-id').value,
    telegram_id: document.getElementById('correction-id').value,
    paid_at: document.getElementById('correction-date').value,
    payment_provider: document.getElementById('correction-provider').value,
    amount_usd: document.getElementById('correction-amount').value,
    provider_payment_id: document.getElementById('correction-provider-id').value,
    note: document.getElementById('correction-note').value
  });
});
function submitWhitelist(enabled) {
  request(document.getElementById(enabled ? 'whitelist-add' : 'whitelist-remove'), 'whitelist-message', 'submitPanelWhitelist', {
    telegram_id: document.getElementById('whitelist-id').value, enabled: enabled
  });
}
document.getElementById('whitelist-add').addEventListener('click', function() { submitWhitelist(true); });
document.getElementById('whitelist-remove').addEventListener('click', function() { submitWhitelist(false); });
</script></body></html>`;

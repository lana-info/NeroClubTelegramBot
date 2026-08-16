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
const SITE_SHEET = 'Доступ к сайту';
const DASHBOARD_SHEET = 'Dashboard';

function installSheetsSyncTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'syncAllSheets') ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger('syncAllSheets').timeBased().everyMinutes(5).create();
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
  if (!body.count) throw new Error('Backend returned no rows for ' + sheetName + '; import snapshot first');
  const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
  if (!sheet) throw new Error('Sheet not found: ' + sheetName);
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  const responseIndex = headerIndex_(body.headers);
  const output = body.rows.map(function(row) {
    return headers.map(function(header) { return responseIndex[header] === undefined ? '' : row[responseIndex[header]]; });
  });
  sheet.getRange(2, 1, Math.max(sheet.getMaxRows() - 1, output.length), headers.length).clearContent();
  sheet.getRange(2, 1, output.length, headers.length).setValues(output);
}

function syncAllSheets() {
  importCurrentSnapshot();
  syncSheetCommands_();
  writeBackendRows_(USERS_SHEET, '/internal/sheets/users');
  writeBackendRows_(SITE_SHEET, '/internal/sheets/site-access');
  writeBackendRows_(DASHBOARD_SHEET, '/internal/sheets/dashboard');
}

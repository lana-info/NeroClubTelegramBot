/**
 * Nero Club — syncs the "Ключи приложений" tab with the self-hosted backend.
 *
 * One-time setup in Extensions → Apps Script:
 * 1. Set BACKEND_URL and ADMIN_API_TOKEN in Script Properties.
 * 2. Run installKeySyncTrigger() once and approve the Google permissions.
 * 3. Run syncAllKeys() once to verify the connection.
 */
const KEY_SHEET_NAME = 'Ключи приложений';
const BACKEND_URL_PROPERTY = 'BACKEND_URL';
const ADMIN_TOKEN_PROPERTY = 'ADMIN_API_TOKEN';

function installKeySyncTrigger() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'syncAllKeys') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  ScriptApp.newTrigger('syncAllKeys').timeBased().everyMinutes(5).create();
}

function syncAllKeys() {
  const properties = PropertiesService.getScriptProperties();
  const backendUrl = properties.getProperty(BACKEND_URL_PROPERTY);
  const adminToken = properties.getProperty(ADMIN_TOKEN_PROPERTY);
  if (!backendUrl || !adminToken) throw new Error('BACKEND_URL and ADMIN_API_TOKEN are required');

  const sheet = SpreadsheetApp.getActive().getSheetByName(KEY_SHEET_NAME);
  if (!sheet) throw new Error('Sheet not found: ' + KEY_SHEET_NAME);
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;

  const headers = values[0];
  const index = {};
  headers.forEach(function(header, column) { index[header] = column; });
  ['key_id', 'app_name', 'key', 'assigned_user_id', 'status', 'key_expires_at', 'action', 'last_result']
    .forEach(function(header) {
      if (index[header] === undefined) throw new Error('Missing header: ' + header);
    });

  const rows = [];
  const sourceRows = [];
  for (let row = 1; row < values.length; row++) {
    const value = values[row];
    if (!value[index.key_id]) continue;
    rows.push({
      key_id: value[index.key_id],
      app_name: value[index.app_name],
      key: value[index.key],
      access_plan: index.access_plan === undefined ? '' : value[index.access_plan],
      assigned_user_id: value[index.assigned_user_id] ? Number(value[index.assigned_user_id]) : null,
      status: value[index.status] || 'issued',
      key_expires_at: value[index.key_expires_at] || null,
      action: value[index.action] || 'none'
    });
    sourceRows.push(row + 1);
  }
  if (!rows.length) return;

  const response = UrlFetchApp.fetch(backendUrl.replace(/\/$/, '') + '/internal/app-keys/sync', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + adminToken },
    payload: JSON.stringify({ rows: rows }),
    muteHttpExceptions: true
  });
  const body = JSON.parse(response.getContentText());
  if (response.getResponseCode() >= 400) throw new Error(body.detail || 'Backend sync failed');

  const resultByRow = {};
  (body.errors || []).forEach(function(error) { resultByRow[error.row] = 'ERROR: ' + error.error; });
  sourceRows.forEach(function(sourceRow, position) {
    const result = resultByRow[position + 2] || 'SYNCED ' + new Date().toISOString();
    sheet.getRange(sourceRow, index.last_result + 1).setValue(result);
  });
}

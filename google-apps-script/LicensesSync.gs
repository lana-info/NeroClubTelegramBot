/**
 * Nero Club — assigns manually generated license-server keys to people who
 * are not necessarily club members.
 *
 * One row is one person + one application. To issue two applications, use
 * two rows with the same Telegram ID. The action is intentionally explicit:
 * issue or revoke. Keys are sent by the bot through "Мои ключи" after sync.
 */
const LICENSE_SHEET_NAME = 'Лицензии';
const LICENSES_BACKEND_URL_PROPERTY = 'BACKEND_URL';
const LICENSES_ADMIN_TOKEN_PROPERTY = 'ADMIN_API_TOKEN';

function licenseHeaderIndex_(headers) {
  const result = {};
  headers.forEach(function(header, column) { result[header] = column; });
  return result;
}

function ensureLicensesSheet() {
  const spreadsheet = SpreadsheetApp.getActive();
  let sheet = spreadsheet.getSheetByName(LICENSE_SHEET_NAME);
  if (!sheet) sheet = spreadsheet.insertSheet(LICENSE_SHEET_NAME);
  const headers = [
    'license_id', 'telegram_id', 'username', 'email', 'product_id',
    'app_name', 'license_key', 'license_term', 'expires_at', 'status',
    'action', 'last_result'
  ];
  const current = sheet.getRange(1, 1, 1, headers.length).getDisplayValues()[0];
  if (current.every(function(value) { return !value; })) {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, headers.length).setFontWeight('bold');
    sheet.autoResizeColumns(1, headers.length);
  }
  return sheet;
}

function syncLicenses() {
  const properties = PropertiesService.getScriptProperties();
  const backendUrl = properties.getProperty(LICENSES_BACKEND_URL_PROPERTY);
  const adminToken = properties.getProperty(LICENSES_ADMIN_TOKEN_PROPERTY);
  if (!backendUrl || !adminToken) throw new Error('BACKEND_URL and ADMIN_API_TOKEN are required');

  const sheet = ensureLicensesSheet();
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;
  const index = licenseHeaderIndex_(values[0]);
  ['license_id', 'telegram_id', 'product_id', 'license_key', 'status', 'action', 'last_result']
    .forEach(function(header) {
      if (index[header] === undefined) throw new Error('Missing header in ' + LICENSE_SHEET_NAME + ': ' + header);
    });

  const rows = [];
  const sourceRows = [];
  for (let row = 1; row < values.length; row++) {
    const value = values[row];
    const action = String(value[index.action] || 'none').toLowerCase();
    if (!action || action === 'none') continue;
    rows.push({
      license_id: value[index.license_id],
      telegram_id: value[index.telegram_id] ? Number(value[index.telegram_id]) : null,
      email: index.email === undefined ? '' : value[index.email],
      product_id: value[index.product_id],
      app_name: index.app_name === undefined ? '' : value[index.app_name],
      license_key: value[index.license_key],
      license_term: index.license_term === undefined ? 'perpetual' : (value[index.license_term] || 'perpetual'),
      expires_at: index.expires_at === undefined ? null : (value[index.expires_at] || null),
      status: value[index.status] || 'issued',
      action: action
    });
    sourceRows.push(row + 1);
  }
  if (!rows.length) return;

  const response = UrlFetchApp.fetch(backendUrl.replace(/\/$/, '') + '/internal/licenses/sync', {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + adminToken },
    payload: JSON.stringify({ rows: rows }),
    muteHttpExceptions: true
  });
  const body = JSON.parse(response.getContentText() || '{}');
  if (response.getResponseCode() >= 400) throw new Error(body.detail || 'License sync failed');

  const resultByRow = {};
  (body.errors || []).forEach(function(error) {
    resultByRow[error.row] = 'ERROR: ' + error.error;
  });
  sourceRows.forEach(function(sourceRow, position) {
    const result = resultByRow[position + 2] || 'SYNCED ' + new Date().toISOString();
    sheet.getRange(sourceRow, index.last_result + 1).setValue(result);
    if (!result.startsWith('ERROR:')) {
      sheet.getRange(sourceRow, index.action + 1).setValue('none');
      sheet.getRange(sourceRow, index.status + 1).setValue(
        String(rows[position].action).toLowerCase() === 'revoke' ? 'revoked' : 'issued'
      );
    }
  });
}

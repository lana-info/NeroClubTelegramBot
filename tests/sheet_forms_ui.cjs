// Run with: node tests/sheet_forms_ui.cjs. No browser or dependencies required.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('google-apps-script/SheetsSync.gs', 'utf8');
const nodes = {};
const calls = [];
const pending = [];
const node = id => nodes[id] ||= {
  value: '', textContent: '', style: {}, dataset: {}, options: [], selectedIndex: 0,
  events: {}, addEventListener(event, callback) { this.events[event] = callback; },
  appendChild(option) { this.options.push(option); },
  set innerHTML(value) { this.options = []; this.value = ''; this.html = value; }
};
const runner = new Proxy({}, {
  get(target, name) {
    if (name === 'withSuccessHandler') return callback => { target.success = callback; return runner; };
    if (name === 'withFailureHandler') return callback => { target.failure = callback; return runner; };
    return payload => {
      calls.push({name, payload});
      pending.push({name, success: target.success, failure: target.failure});
    };
  }
});
const context = vm.createContext({
  document: {getElementById: node, createElement: () => ({dataset: {}})},
  google: {script: {run: runner}}, window: {confirm: () => false}
});
vm.runInContext(source, context);
const html = vm.runInContext('NERO_CLUB_PANEL_HTML', context);
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length);
vm.runInContext(html.match(/<script>([\s\S]+)<\/script>/)[1], context);
for (const id of Object.keys(nodes)) assert(ids.includes(id), `Missing HTML element ${id}`);
node('user-telegram-id').value = '42';
node('user-username').value = '@anna';
node('user-email').value = 'anna@example.org';
node('user-form').events.submit({preventDefault() {}});
assert.equal(calls.at(-1).name, 'submitPanelUser');
assert.equal(calls.at(-1).payload.telegram_id, '42');
assert.equal(node('user-button').disabled, true);
pending.at(-1).success({ok: true, message: 'Пользователь добавлен'});
assert.equal(node('user-button').disabled, false);
node('deletion-id').value = '42';
node('deletion-id').events.change();
const listRequest = pending.at(-1);
assert.equal(listRequest.name, 'lookupPanelPayments');
listRequest.success({payments: [{payment_id: 'p1', paid_at: '2026-01-15', payment_provider: 'stripe', amount_usd: 20}]});
assert.equal(node('deletion-payment-id').options[0].value, 'p1');
node('deletion-payment-id').value = 'p1';
node('deletion-note').value = 'Дубликат';
const beforeCancel = calls.length;
node('deletion-form').events.submit({preventDefault() {}});
assert.equal(calls.length, beforeCancel);
context.window.confirm = () => true;
node('deletion-form').events.submit({preventDefault() {}});
assert.equal(calls.at(-1).name, 'submitPanelPaymentDeletion');
assert.equal(calls.at(-1).payload.confirmed, true);
assert.equal(calls.at(-1).payload.payment_id, 'p1');
pending.at(-1).success({ok: true, message: 'Платёж удалён'});
assert.equal(pending.at(-1).name, 'lookupPanelPayments');
pending.at(-1).success({payments: []});
assert.equal(node('deletion-payment-id').options.length, 0);
assert.match(node('deletion-payment-id').html, /Нет проведённых платежей/);
node('deletion-id').value = '99';
listRequest.success({payments: [{payment_id: 'stale'}]});
assert.equal(node('deletion-payment-id').options.length, 0);
// Verify Apps Script adapters use the new API and normalize payloads.
vm.runInContext(`
  backendRequest_ = function(path, method, payload) {
    adapterCalls.push({path, method, payload});
    return {user: {telegram_id: 42}};
  };
  refreshPanelViews_ = function() {};
`, context);
context.adapterCalls = [];
vm.runInContext("submitPanelUser({telegram_id:'42',username:' @anna ',wordpress_email:' anna@example.org '})", context);
assert.equal(context.adapterCalls[0].path, '/internal/sheets/users');
assert.equal(context.adapterCalls[0].payload.telegram_id, 42);
vm.runInContext("submitPanelPaymentDeletion({telegram_id:'42',payment_id:' p1 ',note:' duplicate ',confirmed:true})", context);
assert.equal(context.adapterCalls[1].path, '/internal/sheets/payment-deletions');
assert.equal(context.adapterCalls[1].payload.payment_id, 'p1');
console.log('Sidebar forms: create, select, confirm/cancel, refresh, stale response, API adapters passed');

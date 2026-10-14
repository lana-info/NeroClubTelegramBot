// Run with: node tests/sheet_user_edits_ui.cjs
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = {};
const calls = [];
let success, failure;
const node = id => nodes[id] ||= {
  value: '', style: {}, textContent: '', addEventListener(name, callback) { this[name] = callback; }
};
const runner = new Proxy({}, {get(target, name) {
  if (name === 'withSuccessHandler') return fn => { success = fn; return runner; };
  if (name === 'withFailureHandler') return fn => { failure = fn; return runner; };
  return payload => calls.push({name, payload, success, failure});
}});
const context = vm.createContext({document: {getElementById: node}, google: {script: {run: runner}}});
vm.runInContext(fs.readFileSync('google-apps-script/SheetsSync.gs', 'utf8'), context);
const html = vm.runInContext('NERO_CLUB_PANEL_HTML', context);
vm.runInContext(html.match(/<script>([\s\S]+)<\/script>/)[1], context);
for (const id of Object.keys(nodes)) assert(html.includes(`id="${id}"`));
node('edit-user-id').value = '42';
node('edit-user-form').submit({preventDefault() {}});
assert.equal(calls.length, 0);
node('edit-user-email').value = 'new@example.org';
node('edit-user-until').value = '2099-02-03';
node('edit-user-form').submit({preventDefault() {}});
assert.equal(calls.at(-1).name, 'submitPanelUserEdit');
assert.equal(calls.at(-1).payload.manual_access_until, '2099-02-03');
assert.equal(node('edit-user-button').disabled, true);
calls.at(-1).failure({message: 'Нет связи'});
assert.equal(node('edit-user-button').disabled, false);
assert.match(node('edit-user-message').textContent, /Нет связи/);
assert.equal(node('edit-user-email').value, 'new@example.org');
node('edit-user-id').input();
assert.equal(node('edit-user-email').value, '');
assert.equal(node('edit-user-until').value, '');
context.adapterCalls = [];
vm.runInContext(`backendRequest_ = function(path, method, payload) {
  adapterCalls.push({path, method, payload}); return {};
}; refreshPanelViews_ = function() {};`, context);
vm.runInContext("submitPanelUserEdit({telegram_id:'42',wordpress_email:' new@example.org '})", context);
assert.equal(context.adapterCalls[0].path, '/internal/sheets/users/42');
assert.equal(context.adapterCalls[0].method, 'patch');
assert.equal(context.adapterCalls[0].payload.wordpress_email, 'new@example.org');
assert.equal(Object.hasOwn(context.adapterCalls[0].payload, 'manual_access_until'), false);
vm.runInContext("submitPanelUserEdit({telegram_id:'42',manual_access_until:'2099-02-03'})", context);
assert.equal(Object.hasOwn(context.adapterCalls[1].payload, 'wordpress_email'), false);
assert.throws(() => vm.runInContext("submitPanelUserEdit({telegram_id:'42'})", context));
vm.runInContext("refreshPanelViews_ = function() { throw new Error('Refresh unavailable'); }", context);
const saved = vm.runInContext("submitPanelUserEdit({telegram_id:'42',wordpress_email:'new@example.org'})", context);
assert.equal(saved.ok, true);
assert.match(saved.message, /сохранены на сервере/);
vm.runInContext(`backendRequest_ = function() {
  return {headers:['telegram_id','username','wordpress_email','provider_paid_until','manual_access_until'], rows:[[42,'anna','new@example.org','2099-01-01','2099-02-03']]};
}`, context);
assert.match(vm.runInContext("lookupPanelUser({telegram_id:'42'})", context).message, /2099-02-03/);
console.log('User edits: submit, validation, failure, user switch, partial patch, saved/refresh failure, effective date passed');

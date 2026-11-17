/* Run with: node --test tests/test_location_permission_ui.cjs. No browser or live API. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness({ permissionState, geolocation } = {}) {
  const status = { state: permissionState, onchange: null };
  const queries = [];
  const context = {
    document: {
      querySelector: selector => ({ '#dialog': { open: false, addEventListener() {} }, '#app': { innerHTML: '' } }[selector] || null),
      querySelectorAll: () => [], addEventListener() {}, activeElement: null,
    },
    window: { scrollTo() {} },
    navigator: {
      geolocation: { getCurrentPosition: geolocation || ((success) => success({ coords: { latitude: 37.5, longitude: 127 }, timestamp: 1700000000000 })) },
      permissions: permissionState ? { query: async (descriptor) => { queries.push(descriptor); return status; } } : undefined,
    },
    localStorage: { getItem() { return null; }, setItem() {} },
    URLSearchParams, AbortController,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    fetch: () => Promise.resolve({ ok: true, json: async () => ({}) }),
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,locationNotice,watchLocationPermission,locate};', context);
  return { ...context.ui, status, queries };
}

test('prompt state shows an allow button without asking the browser yet', () => {
  const ui = harness();
  const html = ui.locationNotice();
  assert.match(html, /data-action="request-location"/);
  assert.match(html, /위치 허용하기/);
  assert.doesNotMatch(html, /차단/);
});

test('denied state explains how to change the browser setting', async () => {
  const ui = harness({ permissionState: 'denied' });
  ui.watchLocationPermission();
  await flush();
  assert.equal(JSON.stringify(ui.queries), '[{"name":"geolocation"}]');
  const html = ui.locationNotice();
  assert.match(html, /위치가 차단되어 있어요/);
  assert.match(html, /자물쇠/);
  assert.match(html, /다시 확인/);
});

test('notice disappears once permission is granted or a location is known', async () => {
  const ui = harness({ permissionState: 'granted' });
  ui.watchLocationPermission();
  await flush();
  assert.equal(ui.locationNotice(), '');
  const other = harness();
  other.state.location = { lat: 37.5, lon: 127, updated_at: 1 };
  assert.equal(other.locationNotice(), '');
});

test('permission changes in the browser settings update the state', async () => {
  const ui = harness({ permissionState: 'denied' });
  ui.watchLocationPermission();
  await flush();
  assert.equal(ui.state.locPerm, 'denied');
  ui.status.state = 'granted';
  ui.status.onchange();
  assert.equal(ui.state.locPerm, 'granted');
});

test('locate records granted on success and denied on a permission error', async () => {
  const ok = harness();
  await ok.locate();
  assert.equal(ok.state.locPerm, 'granted');
  const blocked = harness({ geolocation: (success, failure) => failure({ code: 1 }) });
  await assert.rejects(blocked.locate(), { code: 1 });
  assert.equal(blocked.state.locPerm, 'denied');
  assert.match(blocked.locationNotice(), /위치가 차단되어 있어요/);
});

test('browsers without the Permissions API still work', () => {
  const ui = harness();
  assert.doesNotThrow(() => ui.watchLocationPermission());
  assert.match(ui.locationNotice(), /위치 허용하기/);
});

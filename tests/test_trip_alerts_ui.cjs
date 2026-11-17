/* Run with: node --test tests/test_trip_alerts_ui.cjs. Mocked DOM, clock and API; no GPS or live alerts. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const response = (data, ok = true) => ({ ok, json: async () => data });
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

// 위도 37.5에서 동쪽으로 약 880m 이어지는 직선 경로. 위도 0.001도는 약 110m.
const ROUTE = [[37.5, 127.0], [37.5, 127.005], [37.5, 127.01]];
const T0 = Date.parse('2026-11-18T12:00:00Z');

function harness(fetchImpl) {
  const timers = new Map(), requests = [];
  let nextId = 1;
  const dialog = { open: false, innerHTML: '', addEventListener() {}, showModal() { this.open = true; }, close() { this.open = false; } };
  const count = { textContent: '' };
  const document = {
    querySelector(selector) { return { '#dialog': dialog, '#app': { innerHTML: '' }, '#toast': { classList: { add() {}, remove() {} } }, '#check-count': count }[selector] || null; },
    querySelectorAll: () => [], addEventListener() {},
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: { geolocation: { clearWatch() {}, watchPosition() { return 1; } } },
    localStorage: { getItem() { return null; }, setItem() {} },
    FormData: class {}, AbortController, URLSearchParams,
    setTimeout() { return 1; }, clearTimeout() {}, requestAnimationFrame() {},
    setInterval(fn, ms) { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearInterval(id) { timers.delete(id); },
    async fetch(url, options) { requests.push({ url, body: options.body ? JSON.parse(options.body) : null }); return fetchImpl ? fetchImpl(url, options) : response({ notification: { in_app: 'recorded', sms: { status: 'push' } } }); },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,metersToRoute,tripCheckKind,evaluateTrip,tripWatch,preferences,notificationFeedback,startLocationWatch,stopLocationWatch,closeDialog,askTripCheck};', context);
  const ui = context.ui;
  ui.state.user = { id: 'u', email: 'a@example.com', nickname: '하늘' };
  ui.state.trip = { id: 't1', local: false, share_location: true, destination: { name: '집' }, route: { name: '안심 경로', coordinates: ROUTE },
    eta: new Date(T0 + 20 * 60000).toISOString(), status: 'active' };
  const tick = async (seconds = 1) => { for (let i = 0; i < seconds; i++) for (const t of [...timers.values()].filter(x => x.ms === 1000)) t.fn(); await flush(); };
  return { ...ui, dialog, count, requests, timers, tick };
}

const ON = [37.5, 127.0025];       // 경로 위
const OFF = [37.5015, 127.0025];   // 경로에서 약 165m

test('distance to the route is measured to the nearest segment', () => {
  const ui = harness();
  assert.ok(ui.metersToRoute(...ON, ROUTE) < 1);
  const side = ui.metersToRoute(37.501, 127.0025, ROUTE);
  assert.ok(side > 105 && side < 115, `expected ~110m, got ${side}`);
  const beyond = ui.metersToRoute(37.5, 127.011, ROUTE);
  assert.ok(beyond > 85 && beyond < 95, `expected ~88m past the end, got ${beyond}`);
});

test('leaving the route for under a minute does not trigger anything', () => {
  const ui = harness();
  assert.equal(ui.tripCheckKind(T0, ...OFF, 10), '');
  assert.equal(ui.tripCheckKind(T0 + 59000, ...OFF, 10), '');
});

test('staying off the route for a minute asks first', () => {
  const ui = harness();
  ui.tripCheckKind(T0, ...OFF, 10);
  assert.equal(ui.tripCheckKind(T0 + 60000, ...OFF, 10), 'deviation');
});

test('returning to the route resets the timer', () => {
  const ui = harness();
  ui.tripCheckKind(T0, ...OFF, 10);
  assert.equal(ui.tripCheckKind(T0 + 40000, ...ON, 10), '');
  ui.tripCheckKind(T0 + 50000, ...OFF, 10);
  assert.equal(ui.tripCheckKind(T0 + 100000, ...OFF, 10), '');
  assert.equal(ui.tripCheckKind(T0 + 110000, ...OFF, 10), 'deviation');
});

test('a poor GPS fix cannot start or confirm a deviation', () => {
  const ui = harness();
  assert.equal(ui.tripCheckKind(T0, ...OFF, 200), '');
  assert.equal(ui.tripCheckKind(T0 + 120000, ...OFF, 200), '');
  assert.equal(ui.tripWatch.offSince, 0);
});

test('a deviation is not confirmed from an old position', () => {
  const ui = harness();
  ui.tripCheckKind(T0, ...OFF, 10);
  assert.equal(ui.tripCheckKind(T0 + 200000), '');
});

test('arrival delay is asked only after five minutes past the estimate', () => {
  const ui = harness();
  const eta = Date.parse(ui.state.trip.eta);
  assert.equal(ui.tripCheckKind(eta + 299000, ...ON, 10), '');
  assert.equal(ui.tripCheckKind(eta + 301000, ...ON, 10), 'delay');
});

test('nothing runs without a signed-in, server-side trip', () => {
  const ui = harness();
  ui.state.trip.local = true;
  assert.equal(ui.tripCheckKind(T0 + 9e7, ...OFF, 10), '');
  ui.state.trip.local = false;
  ui.state.user = null;
  assert.equal(ui.tripCheckKind(T0 + 9e7, ...OFF, 10), '');
  ui.state.user = { id: 'u' };
  ui.state.trip = null;
  assert.equal(ui.tripCheckKind(T0 + 9e7, ...OFF, 10), '');
});

test('no response within 30 seconds alerts the guardians once, with an honest message', async () => {
  const ui = harness();
  const eta = Date.parse(ui.state.trip.eta);
  ui.evaluateTrip(eta + 301000, ...ON, 10);
  assert.equal(ui.dialog.open, true);
  assert.match(ui.dialog.innerHTML, /괜찮으신가요/);
  assert.match(ui.dialog.innerHTML, /30초 안에 응답이 없으면/);
  await ui.tick(29);
  assert.equal(ui.requests.length, 0);
  await ui.tick(1);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, '/api/trips/t1/events');
  assert.equal(ui.requests[0].body.type, 'delay');
  assert.match(ui.requests[0].body.message, /5분 넘게 지났고 확인 요청에 응답이 없어요/);
  assert.match(ui.dialog.innerHTML, /보호자에게 알렸어요/);
  assert.equal(ui.tripWatch.alerted.delay, true);
  // 같은 귀가에서 다시 묻거나 보내지 않는다.
  ui.dialog.open = false;
  assert.equal(ui.tripCheckKind(eta + 301000 + 3600000, ...ON, 10), '');
});

test('answering "괜찮아요" sends nothing and waits ten minutes before asking again', async () => {
  const ui = harness();
  ui.state.trip.eta = new Date(Date.now() - 10 * 60000).toISOString();
  const now = Date.now();
  ui.evaluateTrip(now, ...ON, 10);
  assert.equal(ui.dialog.open, true);
  ui.closeDialog();  // "괜찮아요"와 닫기 버튼이 거치는 경로
  await ui.tick(40);
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.tripCheckKind(now + 9 * 60000, ...ON, 10), '');
  assert.equal(ui.tripCheckKind(now + 10 * 60000 + 1000, ...ON, 10), 'delay');
});

test('a deviation alert carries its own message and goes to the trip', async () => {
  const ui = harness();
  ui.tripCheckKind(Date.now() - 61000, ...OFF, 10);
  ui.evaluateTrip(Date.now(), ...OFF, 10);
  assert.equal(ui.dialog.open, true);
  assert.match(ui.dialog.innerHTML, /경로에서 많이 벗어났어요/);
  await ui.tick(30);
  assert.equal(ui.requests[0].body.type, 'deviation');
  assert.match(ui.requests[0].body.message, /100m 넘게 벗어났고/);
});

test('it never interrupts another open dialog such as an SOS countdown', () => {
  const ui = harness();
  ui.state.trip.eta = new Date(Date.now() - 10 * 60000).toISOString();
  ui.dialog.open = true;
  ui.evaluateTrip(Date.now(), ...ON, 10);
  assert.equal(ui.dialog.innerHTML, '');
  assert.equal([...ui.timers.values()].filter(t => t.ms === 1000).length, 0);
});

test('a failed alert is not marked as sent, so it can be retried later', async () => {
  const ui = harness(() => response({ error: '요청이 많아요.' }, false));
  ui.state.trip.eta = new Date(Date.now() - 10 * 60000).toISOString();
  ui.evaluateTrip(Date.now(), ...ON, 10);
  await ui.tick(30);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.tripWatch.alerted.delay, false);
  assert.equal(ui.tripCheckKind(Date.now() + 10 * 60000 + 1000, ...ON, 10), 'delay');
});

test('the same-alert reply from the server is explained to the traveler', () => {
  const ui = harness();
  assert.match(ui.notificationFeedback({ in_app: 'duplicate' }, '알렸어요.'), /같은 알림을 방금/);
});

test('settings describe the automatic alerts truthfully, including the screen-on limit', () => {
  const ui = harness();
  const html = ui.preferences();
  assert.match(html, /이탈·지연 자동 알림/);
  assert.match(html, /100m 넘게 1분 이상/);
  assert.match(html, /5분 넘게/);
  assert.match(html, /30초 안에 응답이 없으면/);
  assert.match(html, /화면이 꺼져 있으면 확인할 수 없어요/);
});

test('the periodic check starts with the location watch and stops with it', () => {
  const ui = harness();
  ui.startLocationWatch();
  assert.equal([...ui.timers.values()].filter(t => t.ms === 15000).length, 1);
  ui.startLocationWatch();
  assert.equal([...ui.timers.values()].filter(t => t.ms === 15000).length, 1);
  ui.stopLocationWatch();
  assert.equal([...ui.timers.values()].filter(t => t.ms === 15000).length, 0);
});

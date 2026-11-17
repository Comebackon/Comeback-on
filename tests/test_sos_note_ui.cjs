/* Run with: node --test tests/test_sos_note_ui.cjs. Mocked DOM, timers and API; nothing is sent. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const response = (data, ok = true) => ({ ok, json: async () => data });
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness() {
  const listeners = new Map(), timers = new Map(), requests = [];
  let nextId = 1;
  const dialog = { open: false, innerHTML: '', addEventListener() {}, showModal() { this.open = true; }, close() { this.open = false; } };
  const note = { id: 'sos-note', value: '' }, count = { textContent: '3' }, hint = { innerHTML: '' };
  const document = {
    querySelector(selector) { return { '#dialog': dialog, '#app': { innerHTML: '' }, '#toast': { classList: { add() {}, remove() {} } }, '#sos-note': note, '#sos-count': count, '#sos-hint': hint }[selector] || null; },
    querySelectorAll: () => [],
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: { geolocation: { clearWatch() {} } },
    localStorage: { getItem() { return null; }, setItem() {} },
    FormData: class {}, AbortController, URLSearchParams,
    setTimeout() { return 1; }, clearTimeout() {}, requestAnimationFrame() {},
    setInterval(fn, ms) { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearInterval(id) { timers.delete(id); },
    async fetch(url, options) { requests.push({ url, body: JSON.parse(options.body) }); return response({ notification: { in_app: 'recorded', sms: { status: 'sent' } } }); },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,requestSos,closeDialog};', context);
  context.ui.state.user = { id: 'u', email: 'a@example.com', nickname: '하늘' };
  context.ui.state.capabilities = { sms: true };
  const fire = (type, target) => Promise.all((listeners.get(type) || []).map(fn => fn({ target, preventDefault() {} })));
  const tick = async n => { for (let i = 0; i < n; i++) for (const t of [...timers.values()].filter(x => x.ms === 1000)) t.fn(); await flush(); };
  return { ...context.ui, dialog, note, count, hint, requests, timers, tick,
    type: value => { note.value = value; return fire('input', note); },
    click: action => fire('click', { closest: () => ({ disabled: false, dataset: { action } }) }) };
}

test('SOS window offers an optional message of up to 100 characters', async () => {
  const ui = harness();
  await ui.requestSos();
  assert.match(ui.dialog.innerHTML, /id="sos-note"[^>]*maxlength="100"/);
  assert.match(ui.dialog.innerHTML, /전하고 싶은 말 \(선택\)/);
  assert.match(ui.dialog.innerHTML, /data-action="sos-send"/);
  assert.doesNotMatch(ui.dialog.innerHTML, /id="sos-note"[^>]*required/);
});

test('without typing anything the request still goes out automatically after three seconds', async () => {
  const ui = harness();
  await ui.requestSos();
  await ui.tick(2);
  assert.equal(ui.requests.length, 0);
  await ui.tick(1);
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, '/api/sos');
  assert.equal(ui.requests[0].body.note, '');
});

test('typing pauses the countdown, and nothing is sent until the person confirms', async () => {
  const ui = harness();
  await ui.requestSos();
  await ui.type('어두운 골목에서 누가 따라와요');
  assert.equal(ui.count.textContent, '✍');
  assert.match(ui.hint.innerHTML, /지금 보내기/);
  await ui.tick(30);
  assert.equal(ui.requests.length, 0);
  await ui.click('sos-send');
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].body.note, '어두운 골목에서 누가 따라와요');
  assert.match(ui.dialog.innerHTML, /도움 요청 처리 결과/);
});

test('the message is trimmed, and "send now" works before typing too', async () => {
  const ui = harness();
  await ui.requestSos();
  ui.note.value = '   다쳤어요  ';
  await ui.click('sos-send');
  assert.equal(ui.requests[0].body.note, '다쳤어요');
  assert.equal([...ui.timers.values()].filter(t => t.ms === 1000).length, 0);
  const again = harness();
  await again.requestSos();
  await again.click('sos-send');
  assert.equal(again.requests[0].body.note, '');
});

test('cancelling sends nothing, even after typing', async () => {
  const ui = harness();
  await ui.requestSos();
  await ui.type('취소할래요');
  ui.closeDialog();
  await ui.tick(10);
  assert.equal(ui.requests.length, 0);
});

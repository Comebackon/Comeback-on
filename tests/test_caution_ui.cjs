/* Run with: node --test tests/test_caution_ui.cjs. Mocked DOM. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');

function harness() {
  const toasts = [], cardEl = { outerHTML: '' };
  const document = {
    querySelector(selector) {
      if (selector === '#caution-card') return cardEl;
      if (selector === '#app') return { innerHTML: '' };
      if (selector === '#dialog') return { addEventListener() {} };
      if (selector === '#toast') return { classList: { add() {}, remove() {} }, set textContent(v) { toasts.push(v); } };
      return null;
    },
    querySelectorAll: () => [], addEventListener() {},
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: { vibrate() {} }, localStorage: { getItem() { return null; }, setItem() {} },
    FormData: class {}, AbortController, URLSearchParams, setTimeout() { return 1; }, clearTimeout() {}, requestAnimationFrame() {}, setInterval() { return 1; }, clearInterval() {},
    fetch: async () => ({ ok: true, json: async () => ({}) }),
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,cautionNear,cautionCard,checkCaution,riskChip};', context);
  return { ...context.ui, toasts, cardEl };
}

const ZONES = [{ lat: 37.5, lon: 127.0, length_m: 100, percent: 41 }, { lat: 37.51, lon: 127.0, length_m: 50, percent: 35 }];
const trip = (zones = ZONES) => ({ id: 't1', route: { coordinates: [[37.49, 127], [37.52, 127]], caution_zones: zones } });

test('cautionNear picks the nearest zone within reach and ignores far or malformed ones', () => {
  const ui = harness();
  assert.equal(ui.cautionNear(37.5, 127.0, ZONES).index, 0);
  assert.equal(ui.cautionNear(37.5101, 127.0, ZONES).index, 1);
  assert.equal(ui.cautionNear(37.505, 127.0, ZONES), null);
  assert.equal(ui.cautionNear(37.5, 127.0, [{ lat: 'x', lon: 1 }, null]), null);
  assert.equal(ui.cautionNear(37.5, 127.0, undefined), null);
});

test('entering a zone shows the card once and toasts only the first time per zone', () => {
  const ui = harness();
  ui.state.trip = trip(); ui.state.screen = 'trip';
  ui.checkCaution(37.5, 127.0, 10);
  assert.equal(ui.state.caution.index, 0);
  assert.match(ui.cardEl.outerHTML, /시설이 적은 구간이에요/);
  assert.equal(ui.toasts.length, 1);
  ui.checkCaution(37.5002, 127.0, 10);
  assert.equal(ui.toasts.length, 1);
  ui.checkCaution(37.505, 127.0, 10);
  assert.equal(ui.state.caution, null);
  ui.checkCaution(37.5, 127.0, 10);
  assert.equal(ui.toasts.length, 1, 're-entering the same zone does not toast again');
});

test('inaccurate fixes and missing trips are ignored; ending a trip clears the card state', () => {
  const ui = harness();
  ui.state.trip = trip(); ui.state.screen = 'trip';
  ui.checkCaution(37.5, 127.0, 500);
  assert.equal(ui.state.caution, null);
  ui.checkCaution(37.5, 127.0, 10);
  assert.ok(ui.state.caution);
  ui.state.trip = null;
  ui.checkCaution(37.5, 127.0, 10);
  assert.equal(ui.state.caution, null);
});

test('card markup is empty-hidden without a hit, and the zone count shows on the risk chip', () => {
  const ui = harness();
  assert.match(ui.cautionCard(), /hidden/);
  assert.match(ui.riskChip({ risk: { percent: 40, level: '높음' }, caution_zones: ZONES }), /주의 구간 2곳/);
  assert.doesNotMatch(ui.riskChip({ risk: { percent: 10, level: '낮음' }, caution_zones: [] }), /주의 구간/);
});

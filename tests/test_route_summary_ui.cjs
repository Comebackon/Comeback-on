/* Run with: node --test tests/test_route_summary_ui.cjs. Mocked DOM and API. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness(fetchImpl) {
  const requests = [];
  const summaryEl = { innerHTML: '', hidden: true };
  const document = {
    querySelector(selector) { return { '#route-summary': summaryEl, '#app': { innerHTML: '' }, '#dialog': { addEventListener() {} }, '#toast': { classList: { add() {}, remove() {} } } }[selector] || null; },
    querySelectorAll: () => [], addEventListener() {},
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: {}, localStorage: { getItem() { return null; }, setItem() {} },
    FormData: class {}, AbortController, URLSearchParams, setTimeout() { return 1; }, clearTimeout() {}, requestAnimationFrame() {}, setInterval() { return 1; }, clearInterval() {},
    async fetch(url, options) { requests.push({ url, body: JSON.parse(options.body) }); return fetchImpl(url, options); },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,riskChip,summaryBlock,loadSummary};', context);
  return { ...context.ui, summaryEl, requests };
}

const ROUTES = [
  { id: 'safe', name: '안심 경로', duration_s: 600, distance_m: 700, facility_counts: { CCTV: 3 }, risk: { percent: 12, level: '낮음' }, secret: 'x' },
  { id: 'basic', name: '최단 경로', duration_s: 480, distance_m: 560, facility_counts: {}, risk: { percent: 45, level: '높음' } },
];
const ok = (data) => ({ ok: true, json: async () => data });

test('risk chip shows percent and level with a colour class', () => {
  const ui = harness(() => ok({}));
  assert.match(ui.riskChip(ROUTES[0]), /위험 지표 12% · 낮음/);
  assert.match(ui.riskChip(ROUTES[0]), /risk-chip low/);
  assert.match(ui.riskChip(ROUTES[1]), /risk-chip high/);
  assert.equal(ui.riskChip({}), '');
});

test('summary block is hidden until a summary arrives', () => {
  const ui = harness(() => ok({}));
  assert.match(ui.summaryBlock(), /hidden/);
  ui.state.summary = { text: '2분 더 걸리지만 CCTV가 더 많아요.', source: 'llm' };
  const html = ui.summaryBlock();
  assert.doesNotMatch(html, /hidden/);
  assert.match(html, /AI 요약/);
});

test('summary request sends only the whitelisted route fields and fills the block', async () => {
  const ui = harness(() => ok({ summary: '요약 문장', source: 'template' }));
  ui.state.routes = ROUTES;
  await ui.loadSummary(ROUTES);
  await flush();
  assert.equal(ui.requests[0].url, '/api/routes/summary');
  assert.deepEqual(Object.keys(ui.requests[0].body.routes[0]).sort(), ['distance_m', 'duration_s', 'facility_counts', 'id', 'risk', 'same_as_shortest']);
  assert.equal(ui.state.summary.text, '요약 문장');
  assert.equal(ui.summaryEl.hidden, false);
  assert.match(ui.summaryEl.innerHTML, /요약 문장/);
});

test('a late answer for old routes is ignored and failures are silent', async () => {
  const ui = harness(() => ok({ summary: '옛 경로 요약', source: 'llm' }));
  ui.state.routes = [];
  await ui.loadSummary(ROUTES);
  assert.equal(ui.state.summary, null);
  const failing = harness(() => ({ ok: false, status: 500, json: async () => ({ error: 'x' }) }));
  failing.state.routes = ROUTES;
  await failing.loadSummary(ROUTES);
  assert.equal(failing.state.summary, null);
});

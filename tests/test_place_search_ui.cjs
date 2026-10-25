/* Run with: node --test tests/test_place_search_ui.cjs. No browser or live API. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const response = data => ({ ok: true, json: async () => data });
const places = name => ({ places: [{ name, address: '서울', lat: 37.5, lon: 127, distance_m: 150 }], sort: 'distance' });

function harness({ geolocation, fetch: fetchImpl } = {}) {
  const clock = { now: 1700000000000 }, timers = new Map(), listeners = new Map(), requests = [], positions = [];
  let timerId = 0, fullRenders = 0;
  const input = { id: 'search-input', value: '', selectionStart: 2, selectionEnd: 2, disabled: false, focus() {} };
  const content = { innerHTML: '', setAttribute() {} };
  const route = { disabled: false };
  const startLabel = { textContent: '' };
  const dialog = { open: false, addEventListener() {} };
  const app = { set innerHTML(value) { fullRenders++; } };
  const document = {
    querySelector: selector => ({ '#search-input': input, '#place-search-content': content, '[data-action="find-route"]': route, '[data-action="search-field"][data-field="start"] .address-value': startLabel, '#dialog': dialog, '#app': app })[selector] || null,
    querySelectorAll: () => [],
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
    activeElement: input,
  };
  const position = () => ({ coords: { latitude: 37.5, longitude: 127 }, timestamp: clock.now });
  const context = {
    document, window: { scrollTo() {} },
    navigator: { geolocation: { getCurrentPosition(success, failure, options) { positions.push(options); if (geolocation) return geolocation(success, failure, options, position); success(position()); } } },
    localStorage: { getItem() { return null; }, setItem() {} },
    URLSearchParams, AbortController,
    FormData: class { constructor(form) { this.form = form; } get(name) { return name === 'q' ? this.form.value : null; } },
    Date: class extends Date { static now() { return clock.now; } },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, at: clock.now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); }, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    fetch(url, options) {
      requests.push({ url, options });
      if (fetchImpl) return fetchImpl(url, options);
      const query = new URLSearchParams(url.split('?')[1]);
      const result = places(query.get('q'));
      if (!query.has('lat')) { result.sort = 'relevance'; delete result.places[0].distance_m; }
      return Promise.resolve(response(result));
    },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,findPlaces,queuePlaceSearch,cancelPlaceSearch,placeSearchContent,search,navigate,locate};', context);
  context.ui.state.screen = 'search';
  const fire = (type, extra = {}) => Promise.all((listeners.get(type) || []).map(fn => fn({ target: input, ...extra })));
  const advance = async milliseconds => {
    const end = clock.now + milliseconds;
    while (true) {
      const next = [...timers.entries()].filter(([, item]) => item.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]); clock.now = next[1].at; next[1].callback(); await flush();
    }
    clock.now = end; await flush();
  };
  return { ...context.ui, input, content, route, startLabel, document, requests, positions, fire, advance, position, renders: () => fullRenders };
}

test('Korean composition waits for completion and one debounce, preserves input while showing 20 matches', async () => {
  const h = harness({ fetch: () => Promise.resolve(response({ sort: 'distance', places: Array.from({ length: 20 }, (_, i) => ({ ...places('엽기떡볶이 ' + i).places[0] })) })) });
  h.state.start = { name: '선택한 출발지', lat: 35, lon: 128 };
  h.input.value = '엽'; await h.fire('compositionstart'); await h.fire('input', { isComposing: true });
  await h.advance(1000); assert.equal(h.requests.length, 0); assert.equal(h.positions.length, 0);
  h.input.value = '엽기'; await h.fire('compositionend'); await h.fire('input', { isComposing: false });
  await h.advance(599); assert.equal(h.requests.length, 0);
  await h.advance(1); assert.equal(h.requests.length, 1);
  const query = new URLSearchParams(h.requests[0].url.split('?')[1]);
  assert.equal(query.get('q'), '엽기'); assert.equal(query.get('lat'), '37.5'); assert.equal(query.get('lon'), '127');
  assert.equal(h.state.searchResults.length, 20);
  assert.equal((h.content.innerHTML.match(/data-action="choose-result"/g) || []).length, 20);
  assert.match(h.content.innerHTML, /가까운 순 · 직선거리 기준/);
  assert.match(h.content.innerHTML, /내 위치에서 150m/);
  assert.equal(h.state.start.name, '선택한 출발지');
  assert.equal(h.document.activeElement, h.input); assert.equal(h.input.selectionStart, 2);
  assert.equal(h.input.disabled, false); assert.equal(h.renders(), 0);
});

test('successive typed queries share the pending position and only latest query reaches API', async () => {
  let succeed;
  const h = harness({ geolocation: success => { succeed = success; } });
  h.input.value = '엽기'; await h.fire('input'); await h.advance(600);
  h.input.value = '엽기떡'; await h.fire('input'); await h.advance(600);
  assert.equal(h.positions.length, 1); assert.equal(h.requests.length, 0);
  succeed(h.position()); await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(new URLSearchParams(h.requests[0].url.split('?')[1]).get('q'), '엽기떡');
});

test('later response wins, earlier fetch aborts, and a late error cannot replace newer results', async () => {
  const pending = [];
  const h = harness({ fetch: () => { const request = deferred(); pending.push(request); return request.promise; } });
  const first = h.findPlaces('엽기', { automatic: true }); await flush();
  const second = h.findPlaces('다이소', { automatic: true }); await flush();
  assert.equal(h.requests[0].options.signal.aborted, true);
  pending[1].resolve(response(places('다이소'))); await second;
  pending[0].reject(new Error('이전 요청 실패')); await first;
  assert.equal(h.state.searchResults[0].name, '다이소'); assert.equal(h.state.error, '');
  assert.equal(h.positions.length, 1); assert.equal(h.renders(), 0);
});

test('clearing input or leaving search prevents an in-flight result from returning', async () => {
  const pending = [];
  const h = harness({ fetch: () => { const request = deferred(); pending.push(request); return request.promise; } });
  const first = h.findPlaces('엽기', { automatic: true }); await flush();
  h.input.value = ''; await h.fire('input');
  pending[0].resolve(response(places('엽기'))); await first;
  assert.equal(h.state.searchResults, null); assert.equal(h.state.query, '');
  const second = h.findPlaces('학교', { automatic: true }); await flush();
  await h.navigate('home'); pending[1].resolve(response(places('학교'))); await second;
  assert.equal(h.state.screen, 'home'); assert.equal(h.state.searchResults, null);
});

test('denied position is not retried on every keystroke; explicit search retries', async () => {
  let count = 0;
  const h = harness({ geolocation(success, failure, options, position) { if (++count === 1) failure({ code: 1 }); else success(position()); } });
  await h.findPlaces('엽기', { automatic: true });
  assert.match(h.state.searchLocationNotice, /위치 권한이 꺼져/); assert.equal(h.state.searchSort, 'relevance');
  await h.findPlaces('카페', { automatic: true }); assert.equal(h.positions.length, 1);
  assert.equal(new URLSearchParams(h.requests[1].url.split('?')[1]).has('lat'), false);
  await h.findPlaces('편의점'); assert.equal(h.positions.length, 2); assert.equal(h.state.searchSort, 'distance');
});

test('manual position success clears remembered failure for automatic searches', async () => {
  let count = 0;
  const h = harness({ geolocation(success, failure, options, position) { if (++count === 1) failure({ code: 2 }); else success(position()); } });
  await h.findPlaces('엽기', { automatic: true });
  await h.locate();
  await h.findPlaces('카페', { automatic: true });
  assert.equal(h.positions.length, 2); assert.equal(h.state.searchSort, 'distance');
  assert.equal(h.state.searchLocationNotice, '');
});

test('one character waits for submit, duplicate submits coalesce, and cache hits do not extend expiry', async () => {
  const h = harness();
  h.input.value = '역'; await h.fire('input'); await h.advance(1000); assert.equal(h.requests.length, 0);
  const first = h.findPlaces('역'); const duplicate = h.findPlaces('역'); await Promise.all([first, duplicate]);
  assert.equal(h.requests.length, 1);
  await h.advance(50000); await h.findPlaces('역'); assert.equal(h.requests.length, 1);
  await h.advance(11000); await h.findPlaces('역'); assert.equal(h.requests.length, 2);
});

for (const fails of [false, true]) test(`late favorites ${fails ? 'failure' : 'success'} cannot replace input or active search results`, async () => {
  const favorites = deferred();
  const h = harness({ fetch: url => url === '/api/favorites' ? favorites.promise : Promise.resolve(response(places('엽기떡볶이'))) });
  h.state.user = { nickname: '사용자' };
  const navigation = h.navigate('search');
  assert.equal(h.renders(), 1);
  h.input.value = '엽기'; await h.fire('input'); await h.advance(600);
  assert.equal(h.state.searchResults[0].name, '엽기떡볶이');
  if (fails) favorites.reject(new Error('저장 장소 불러오기 실패'));
  else favorites.resolve(response({ favorites: [{ name: '저장한 학교' }] }));
  await navigation;
  assert.equal(h.renders(), 1); assert.equal(h.document.activeElement, h.input);
  assert.equal(h.input.value, '엽기'); assert.equal(h.input.selectionStart, 2);
  assert.equal(h.state.error, ''); assert.equal(h.state.searchResults[0].name, '엽기떡볶이');
});

test('favorites error without a query is shown without replacing the search input', async () => {
  const favorites = deferred();
  const h = harness({ fetch: () => favorites.promise });
  h.state.user = { nickname: '사용자' };
  const navigation = h.navigate('search');
  favorites.reject(new Error('저장 장소 불러오기 실패')); await navigation;
  assert.equal(h.renders(), 1); assert.match(h.content.innerHTML, /연결이 끊겼어요/);
  assert.equal(h.document.activeElement, h.input);
});

test('manual current-location completion updates start label without interrupting typed query', async () => {
  let succeed;
  const h = harness({ geolocation: success => { succeed = success; } });
  h.state.start = { name: '이전에 선택한 출발지', lat: 35, lon: 128 };
  const button = { disabled: false, dataset: { action: 'use-current' } };
  const action = h.fire('click', { target: { closest: () => button } });
  h.input.value = '엽기'; await h.fire('input'); await h.advance(300);
  succeed(h.position()); await action;
  assert.equal(h.startLabel.textContent, '현재 위치'); assert.equal(h.state.start.lat, 37.5);
  assert.equal(h.renders(), 0); assert.equal(h.input.value, '엽기'); assert.equal(h.input.selectionStart, 2);
  assert.equal(button.disabled, false);
  await h.advance(300); assert.equal(h.positions.length, 1); assert.equal(h.requests.length, 1);
});

/* Run with: node --test tests/test_profile_ui.cjs. Mocked DOM and API; no live accounts. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const response = (data, ok = true) => ({ ok, json: async () => data });

function harness(fetchImpl) {
  const listeners = new Map(), requests = [], toasts = [];
  const dialog = { open: false, innerHTML: '', addEventListener() {}, showModal() { this.open = true; }, close() { this.open = false; } };
  const app = { innerHTML: '' }, toast = { set textContent(value) { toasts.push(value); }, classList: { add() {}, remove() {} } };
  const avatarInput = { id: 'avatar-input', value: 'C:\\fakepath\\me.png', files: [], click() { this.clicked = true; } };
  const nickname = { name: 'nickname', value: '  새이름 ' };
  const form = { id: 'profile-form', fields: { nickname }, querySelector: () => ({ disabled: false }), querySelectorAll: () => [] };
  const document = {
    querySelector(selector) { return { '#app': app, '#dialog': dialog, '#toast': toast, '#avatar-input': avatarInput }[selector] || null; },
    querySelectorAll: () => [],
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: { geolocation: { clearWatch() {} } },
    localStorage: { getItem() { return null; }, setItem() {} },
    FormData: class { constructor(element) { this.form = element; this.parts = {}; } get(name) { return this.form?.fields?.[name]?.value ?? null; } append(key, value) { this.parts[key] = value; } },
    AbortController, URLSearchParams,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    async fetch(url, options) {
      requests.push({ url, method: options.method, body: options.body });
      return fetchImpl ? fetchImpl(url, options) : response({});
    },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,settings,guardianCard,avatarView};', context);
  context.ui.state.screen = 'settings';
  const fire = (type, target) => Promise.all((listeners.get(type) || []).map(fn => fn({ target, preventDefault() {} })));
  return { ...context.ui, requests, toasts, dialog, app, avatarInput, form, nickname,
    click: action => fire('click', { closest: () => ({ disabled: false, dataset: { action } }) }),
    submit: () => fire('submit', form),
    pick: file => { avatarInput.files = file ? [file] : []; return fire('change', avatarInput); } };
}

const member = { id: 'u1', email: 'a@example.com', nickname: '하늘', avatar_url: null };

test('settings offers photo and nickname changes only when signed in', () => {
  const ui = harness();
  ui.state.user = null;
  assert.doesNotMatch(ui.settings(), /avatar-pick|nickname-edit/);
  ui.state.user = { ...member };
  const html = ui.settings();
  assert.match(html, /data-action="avatar-pick"/);
  assert.match(html, /data-action="nickname-edit"/);
  assert.match(html, /id="avatar-input"[^>]*accept="image\/png,image\/jpeg"/);
  assert.doesNotMatch(html, /avatar-delete/);
});

test('a saved photo is shown and can be removed', () => {
  const ui = harness();
  ui.state.user = { ...member, avatar_url: '/api/avatar/u1?v=abc' };
  const html = ui.settings();
  assert.match(html, /<img class="avatar-img" src="\/api\/avatar\/u1\?v=abc"/);
  assert.match(html, /data-action="avatar-delete"/);
});

test('connected people show their photo on the guardian card, others keep the icon', () => {
  const ui = harness();
  const card = person => ui.guardianCard({ id: 'g', status: 'accepted', direction: 'outgoing', person });
  assert.match(card({ nickname: '민수', email: 'm@x.com', avatar_url: '/api/avatar/u2?v=1' }), /class="avatar-img" src="\/api\/avatar\/u2\?v=1"/);
  assert.doesNotMatch(card({ nickname: '민수', email: 'm@x.com', avatar_url: null }), /avatar-img/);
});

test('photo button opens the file chooser', async () => {
  const ui = harness();
  await ui.click('avatar-pick');
  assert.equal(ui.avatarInput.clicked, true);
});

test('choosing a photo uploads it and updates the profile', async () => {
  const ui = harness(() => response({ user: { ...member, avatar_url: '/api/avatar/u1?v=new' }, csrf_token: 'c2' }));
  ui.state.user = { ...member };
  await ui.pick({ name: 'me.png', size: 1000 });
  assert.equal(ui.requests[0].url, '/api/profile/photo');
  assert.equal(ui.requests[0].method, 'POST');
  assert.equal(ui.requests[0].body.parts.photo.name, 'me.png');
  assert.equal(ui.state.user.avatar_url, '/api/avatar/u1?v=new');
  assert.equal(ui.avatarInput.value, '');
  assert.deepEqual(ui.toasts.at(-1), '프로필 사진을 바꿨어요.');
});

test('photos over 3MB are stopped before any upload', async () => {
  const ui = harness();
  await ui.pick({ name: 'big.jpg', size: 3 * 1024 * 1024 + 1 });
  assert.equal(ui.requests.length, 0);
  assert.match(ui.toasts.at(-1), /3MB 이하/);
});

test('cancelling the chooser does nothing and upload errors are shown', async () => {
  const ui = harness(() => response({ error: '사진 파일을 읽을 수 없어요.' }, false));
  await ui.pick(null);
  assert.equal(ui.requests.length, 0);
  await ui.pick({ name: 'x.png', size: 10 });
  assert.match(ui.toasts.at(-1), /사진 파일을 읽을 수 없어요/);
});

test('removing the photo calls the server and clears it', async () => {
  const ui = harness(() => response({ user: { ...member, avatar_url: null } }));
  ui.state.user = { ...member, avatar_url: '/api/avatar/u1?v=1' };
  await ui.click('avatar-delete');
  assert.deepEqual([ui.requests[0].url, ui.requests[0].method], ['/api/profile/photo', 'DELETE']);
  assert.equal(ui.state.user.avatar_url, null);
});

test('nickname dialog is limited to ten characters and saves the trimmed name', async () => {
  const ui = harness(() => response({ user: { ...member, nickname: '새이름' } }));
  ui.state.user = { ...member };
  await ui.click('nickname-edit');
  assert.match(ui.dialog.innerHTML, /id="profile-form"/);
  assert.match(ui.dialog.innerHTML, /maxlength="10"/);
  assert.match(ui.dialog.innerHTML, /value="하늘"/);
  await ui.submit();
  assert.deepEqual([ui.requests[0].url, ui.requests[0].method], ['/api/profile', 'PATCH']);
  assert.deepEqual(JSON.parse(ui.requests[0].body), { nickname: '새이름' });
  assert.equal(ui.state.user.nickname, '새이름');
  assert.equal(ui.dialog.open, false);
});

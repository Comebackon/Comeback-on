/* Run with: node --test tests/test_auth_ui.cjs. Mocked DOM and API; no live accounts. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const response = (data, ok = true) => ({ ok, json: async () => data });
const member = { id: 'fixture-member', email: 'walker@example.com', nickname: '귀가자' };
const signedIn = () => ({ user: member, csrf_token: 'fixture-csrf', capabilities: { email_login: true }, settings: { avoid_stairs: true } });

function harness(fetchImpl) {
  const listeners = new Map(), requests = [], writes = [], toasts = [];
  const email = { name: 'email', value: member.email, disabled: false };
  const password = { name: 'password', value: 'fixture-password-123', disabled: false };
  const nickname = { name: 'nickname', value: member.nickname, disabled: false };
  const submit = { disabled: false }, modeButton = { disabled: false, dataset: { action: 'auth-mode' } };
  const form = { id: 'auth-form', fields: { email, password, nickname }, querySelector: () => submit, querySelectorAll: () => [email, password, nickname, submit] };
  const app = { innerHTML: '' }, dialog = { open: false, addEventListener() {} };
  const toast = { set textContent(value) { toasts.push(value); }, classList: { add() {}, remove() {} } };
  const document = {
    querySelector(selector) { return { '#app': app, '#dialog': dialog, '#toast': toast, '[data-action="auth-mode"]': modeButton, '#auth-form [name="email"]': email }[selector] || null; },
    querySelectorAll: () => [],
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: { geolocation: { clearWatch() {} } },
    localStorage: { getItem() { return null; }, setItem(key, value) { writes.push({ key, value }); } },
    FormData: class { constructor(element) { this.form = element; } get(name) { return this.form.fields[name]?.value ?? null; } },
    AbortController, URLSearchParams,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    async fetch(url, options) {
      requests.push({ url, options });
      if (fetchImpl) return fetchImpl(url, options);
      return response(url === '/api/trips' ? { trip: null } : url === '/api/auth/logout' ? { user: null, csrf_token: 'fixture-logged-out' } : signedIn());
    },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,auth,navigate};', context);
  context.ui.state.screen = 'auth';
  const fire = (type, target) => Promise.all((listeners.get(type) || []).map(fn => fn({ target, preventDefault() {} })));
  return { ...context.ui, requests, writes, toasts, app, form, email, password, submit, modeButton,
    submitForm: () => fire('submit', form),
    click: (action, dataset = {}) => fire('click', { closest: () => ({ disabled: false, dataset: { action, ...dataset } }) }),
    inputEmail: value => { email.value = value; return fire('input', email); },
  };
}

test('successful signup then logout returns to login, never another signup form', async () => {
  const h = harness();
  h.state.authMode = 'register';
  await h.submitForm();
  assert.equal(h.state.user.id, member.id);
  assert.equal(h.state.authMode, 'login');
  assert.equal(h.state.csrf, 'fixture-csrf');
  assert.equal(h.state.settings.avoid_stairs, true);
  assert.ok(h.toasts.includes('가입했어요. 반가워요!'));
  await h.click('logout');
  await h.navigate('auth');
  assert.equal(h.state.authMode, 'login');
  assert.match(h.app.innerHTML, /이메일로 로그인/);
  assert.doesNotMatch(h.app.innerHTML, /name="nickname"/);
});

test('reentering authentication after abandoning signup opens login and retains only email', async () => {
  const h = harness();
  h.state.authMode = 'register';
  await h.inputEmail('returning@example.com');
  await h.navigate('home');
  await h.navigate('auth');
  assert.equal(h.state.authMode, 'login');
  assert.match(h.auth(), /value="returning@example.com"/);
  assert.doesNotMatch(h.auth(), /name="password"[^>]*value=/);
  assert.equal(h.writes.length, 0);
});

test('successful signup is applied before optional trip fetch, which cannot turn it into a signup failure', async () => {
  const trip = deferred();
  const h = harness(url => url === '/api/trips' ? trip.promise : Promise.resolve(response(signedIn())));
  h.state.authMode = 'register';
  const pending = h.submitForm();
  await flush();
  assert.equal(h.state.user.id, member.id);
  assert.equal(h.state.screen, 'home');
  assert.equal(h.state.authPending, false);
  assert.equal(h.state.error, '');
  assert.deepEqual(h.requests.map(r => r.url), ['/api/auth/register', '/api/trips']);
  trip.reject(new Error('fixture trip request failed'));
  await pending;
  assert.equal(h.state.user.id, member.id);
  assert.equal(h.state.screen, 'home');
  assert.equal(h.state.error, '');
  assert.match(h.toasts.at(-1), /로그인은 완료됐어요/);
  assert.doesNotMatch(h.app.innerHTML, /id="auth-form"/);
});

test('pending auth ignores duplicate submits and mode switches and uses the original operation', async () => {
  const registration = deferred();
  const h = harness(url => url === '/api/auth/register' ? registration.promise : Promise.resolve(response({ trip: null })));
  h.state.authMode = 'register';
  h.email.value = '  walker@example.com  ';
  const pending = h.submitForm();
  await flush();
  assert.equal(h.state.authPending, true);
  assert.equal(h.submit.disabled, true);
  assert.equal(h.modeButton.disabled, true);
  await h.click('auth-mode');
  await h.submitForm();
  assert.equal(h.state.authMode, 'register');
  assert.equal(h.requests.length, 1);
  const payload = JSON.parse(h.requests[0].options.body);
  assert.equal(payload.email, member.email);
  assert.equal(payload.password, h.password.value);
  assert.equal(payload.nickname, member.nickname);
  await h.navigate('home');
  await h.navigate('auth');
  assert.equal(h.state.authMode, 'login');
  registration.resolve(response(signedIn()));
  await pending;
  assert.equal(h.state.authMode, 'login');
  assert.ok(h.toasts.includes('가입했어요. 반가워요!'));
  assert.equal(h.requests.filter(r => r.url.startsWith('/api/auth/')).length, 1);
});

test('rejected login preserves email and allows retry without storing the password or switching to signup', async () => {
  let first = true;
  const h = harness(url => {
    if (url === '/api/trips') return response({ trip: null });
    if (first) { first = false; return response({ error: '이메일 또는 비밀번호를 확인해 주세요.' }, false); }
    return response(signedIn());
  });
  h.email.value = '  walker@example.com  ';
  await h.submitForm();
  assert.equal(h.state.user, null);
  assert.equal(h.state.authMode, 'login');
  assert.equal(h.state.authPending, false);
  assert.equal(h.submit.disabled, false);
  assert.equal(h.modeButton.disabled, false);
  assert.match(h.app.innerHTML, /value="walker@example.com"/);
  assert.match(h.state.error, /비밀번호를 확인/);
  assert.equal(JSON.stringify(h.state).includes(h.password.value), false);
  assert.doesNotMatch(h.app.innerHTML, /name="password"[^>]*value=/);
  assert.deepEqual(h.writes, []);
  await h.submitForm();
  assert.equal(h.state.user.id, member.id);
  assert.equal(h.requests.filter(r => r.url === '/api/auth/register').length, 0);
});

for (const failure of [false, true]) test(`late trip ${failure ? 'error' : 'response'} after logout cannot restore account data or overwrite logout feedback`, async () => {
  const trip = deferred();
  const h = harness(url => url === '/api/trips' ? trip.promise : Promise.resolve(response(url === '/api/auth/logout' ? { user: null } : signedIn())));
  const pending = h.submitForm();
  await flush();
  await h.click('logout');
  if (failure) trip.reject(new Error('late failure'));
  else trip.resolve(response({ trip: { id: 'old-account-trip' } }));
  await pending;
  assert.equal(h.state.user, null);
  assert.equal(h.state.trip, null);
  assert.equal(h.toasts.at(-1), '로그아웃했어요.');
});

test('logout during destination loading ends old login completion before optional trip lookup', async () => {
  const favorites = deferred();
  const h = harness(url => url === '/api/favorites' ? favorites.promise : Promise.resolve(response(url === '/api/auth/logout' ? { user: null } : signedIn())));
  h.state.afterLogin = 'favorites';
  const pending = h.submitForm();
  await flush();
  assert.equal(h.state.screen, 'favorites');
  await h.click('logout');
  favorites.resolve(response({ favorites: [] }));
  await pending;
  assert.equal(h.state.user, null);
  assert.equal(h.state.screen, 'home');
  assert.equal(h.toasts.at(-1), '로그아웃했어요.');
  assert.equal(h.requests.some(r => r.url === '/api/trips'), false);
});

test('optional trip lookup cannot erase a newer trip started by the same account', async () => {
  const trip = deferred();
  const h = harness(url => url === '/api/trips' ? trip.promise : Promise.resolve(response(signedIn())));
  const pending = h.submitForm();
  await flush();
  h.state.trip = { id: 'newly-started-trip' };
  trip.resolve(response({ trip: null }));
  await pending;
  assert.equal(h.state.trip.id, 'newly-started-trip');
});

test('explicitly unavailable email login hides forms and blocks submits without showing configuration details', async () => {
  const h = harness();
  assert.match(h.auth(), /id="auth-form"/); // Startup capabilities may still be undefined.
  h.state.capabilities = { email_login: false, account_storage_ready: false };
  assert.doesNotMatch(h.auth(), /id="auth-form"|name="password"|data-action="auth-mode"/);
  assert.match(h.auth(), /로그인과 회원가입을 잠시 이용할 수 없어요/);
  assert.doesNotMatch(h.auth(), /Render|SQLite|COMEBACK|데이터베이스|영구/);
  await h.submitForm();
  assert.equal(h.requests.length, 0);
  h.state.capabilities.email_login = true;
  assert.match(h.auth(), /id="auth-form"/);
});

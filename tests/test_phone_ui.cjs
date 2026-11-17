/* Run with: node --test tests/test_phone_ui.cjs. Mocked DOM and API; no live SMS. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');
const response = (data, ok = true) => ({ ok, json: async () => data });
const member = { id: 'fixture-member', email: 'walker@example.com', nickname: '귀가자' };

function harness(fetchImpl) {
  const listeners = new Map(), requests = [];
  const phone = { name: 'phone', value: '010-1234-5678' }, code = { name: 'phone_code', value: '123456' };
  const email = { name: 'email', value: member.email, disabled: false }, password = { name: 'password', value: 'fixture-password-123', disabled: false };
  const nickname = { name: 'nickname', value: member.nickname, disabled: false }, consent = { name: 'consent', value: 'on' };
  const submit = { disabled: false };
  const form = { id: 'auth-form', fields: { email, password, nickname, consent }, querySelector: () => submit, querySelectorAll: () => [email, password, nickname, submit] };
  const app = { innerHTML: '' }, dialog = { open: false, addEventListener() {} };
  const box = { outerHTML: '' };
  const toast = { set textContent(value) {}, classList: { add() {}, remove() {} } };
  const document = {
    querySelector(selector) {
      return { '#app': app, '#dialog': dialog, '#toast': toast, '#phone-box': box, '#phone-box [name="phone"]': phone,
        '#phone-box [name="phone_code"]': code, '#auth-form [name="email"]': email }[selector] || null;
    },
    querySelectorAll: () => [],
    addEventListener(type, listener) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(listener); },
  };
  const context = {
    document, window: { scrollTo() {} }, navigator: { geolocation: { clearWatch() {} } },
    localStorage: { getItem() { return null; }, setItem() {} },
    FormData: class { constructor(element) { this.form = element; } get(name) { return this.form.fields[name]?.value ?? null; } },
    AbortController, URLSearchParams,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    async fetch(url, options) {
      requests.push({ url, body: options.body ? JSON.parse(options.body) : null });
      return fetchImpl ? fetchImpl(url, options) : response({});
    },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,auth,phoneBox,smsContactCard,freshPhone,guardians,settings,registerMessage};', context);
  context.ui.state.screen = 'auth';
  const fire = (type, target) => Promise.all((listeners.get(type) || []).map(fn => fn({ target, preventDefault() {} })));
  return { ...context.ui, requests, box, phone, code, consent, form,
    click: action => fire('click', { closest: () => ({ disabled: false, dataset: { action } }) }),
    submitForm: () => fire('submit', form) };
}

test('signup form asks for a verified phone only when the SMS service is on', () => {
  const ui = harness();
  ui.state.authMode = 'register';
  ui.state.capabilities = { email_login: true, phone_verification: false };
  assert.doesNotMatch(ui.auth(), /id="phone-box"/);
  ui.state.capabilities = { email_login: true, phone_verification: true };
  const html = ui.auth();
  assert.match(html, /id="phone-box"/);
  assert.match(html, /인증번호 받기/);
  assert.match(html, /name="consent"/);
  ui.state.authMode = 'login';
  assert.doesNotMatch(ui.auth(), /id="phone-box"/);
});

test('code field appears only after a code was sent, and the verified state locks the number', () => {
  const ui = harness();
  assert.doesNotMatch(ui.phoneBox(), /name="phone_code"/);
  ui.state.phoneAuth.step = 'sent';
  assert.match(ui.phoneBox(), /name="phone_code"/);
  assert.match(ui.phoneBox(), /다시 받기/);
  ui.state.phoneAuth = { ...ui.state.phoneAuth, step: 'verified', value: '01012345678' };
  const html = ui.phoneBox();
  assert.match(html, /readonly/);
  assert.match(html, /휴대폰 인증이 끝났어요/);
  assert.match(html, /data-action="phone-reset"/);
  assert.doesNotMatch(html, /name="phone_code"/);
});

test('sending then verifying posts the number and code and keeps the proof', async () => {
  const ui = harness(url => response(url === '/api/phone/verify' ? { verified: true, proof: 'proof-abc' } : { sent: true }));
  await ui.click('phone-send');
  assert.deepEqual(ui.requests[0], { url: '/api/phone/send', body: { phone: '010-1234-5678' } });
  assert.equal(ui.state.phoneAuth.step, 'sent');
  await ui.click('phone-verify');
  assert.deepEqual(ui.requests[1], { url: '/api/phone/verify', body: { phone: '010-1234-5678', code: '123456' } });
  assert.equal(ui.state.phoneAuth.step, 'verified');
  assert.equal(ui.state.phoneAuth.proof, 'proof-abc');
});

test('server errors are shown and do not advance the step', async () => {
  const ui = harness(() => response({ error: '인증번호가 맞지 않아요. 다시 확인해 주세요.' }, false));
  ui.state.phoneAuth.step = 'sent';
  await ui.click('phone-verify');
  assert.equal(ui.state.phoneAuth.step, 'sent');
  assert.equal(ui.state.phoneAuth.proof, '');
  assert.match(ui.state.phoneAuth.error, /인증번호가 맞지 않아요/);
  assert.match(ui.box.outerHTML, /인증번호가 맞지 않아요/);
  assert.equal(ui.state.phoneAuth.busy, false);
});

test('changing the number discards the earlier verification', async () => {
  const ui = harness();
  ui.state.phoneAuth = { ...ui.state.phoneAuth, step: 'verified', value: '01012345678', proof: 'p' };
  await ui.click('phone-reset');
  assert.deepEqual(JSON.parse(JSON.stringify(ui.state.phoneAuth)), JSON.parse(JSON.stringify(ui.freshPhone())));
});

test('signup is blocked until the number is verified and consent is given', async () => {
  const ui = harness();
  ui.state.authMode = 'register';
  ui.state.capabilities = { email_login: true, phone_verification: true };
  await ui.submitForm();
  assert.equal(ui.requests.length, 0);
  assert.match(ui.state.error, /휴대폰 번호 인증을 먼저/);
  ui.state.phoneAuth = { ...ui.state.phoneAuth, step: 'verified', value: '01012345678', proof: 'proof-abc' };
  ui.consent.value = null;
  await ui.submitForm();
  assert.equal(ui.requests.length, 0);
  assert.match(ui.state.error, /동의/);
});

test('verified signup sends the number, proof and consent with the account', async () => {
  const ui = harness(url => response(url === '/api/auth/register'
    ? { user: member, csrf_token: 'c', capabilities: { email_login: true }, settings: {} } : { trip: null, notifications: [] }));
  ui.state.authMode = 'register';
  ui.state.capabilities = { email_login: true, phone_verification: true };
  ui.state.phoneAuth = { ...ui.state.phoneAuth, step: 'verified', value: '01012345678', proof: 'proof-abc' };
  await ui.submitForm();
  const register = ui.requests.find(r => r.url === '/api/auth/register');
  assert.ok(register, 'register request was sent');
  assert.equal(register.body.phone, '01012345678');
  assert.equal(register.body.phone_proof, 'proof-abc');
  assert.equal(register.body.consent, true);
  assert.equal(ui.state.phoneAuth.step, 'idle');
});

test('number card on the guardian screen uses the same verification box', () => {
  const ui = harness();
  ui.state.capabilities = { sms: true, phone_verification: true };
  const html = ui.smsContactCard();
  assert.match(html, /id="phone-box"/);
  assert.match(html, /인증번호 받기/);
  assert.match(html, /name="consent" required/);
});

test('guardian screen no longer asks for a phone number; settings holds the number card', () => {
  const ui = harness();
  ui.state.user = { id: 'u', email: 'a@example.com', nickname: '하늘', avatar_url: null };
  ui.state.capabilities = { sms: true, phone_verification: true };
  ui.state.smsContact = { available: true, phone: '010-****-5678' };
  assert.doesNotMatch(ui.guardians(), /sms-card|문자 알림 받기|sms-contact/);
  const html = ui.settings();
  assert.match(html, /문자 알림 받기/);
  assert.match(html, /010-\*\*\*\*-5678/);
  assert.match(html, /data-action="sms-contact-delete"/);
});

test('signup form has an optional guardian email that can be skipped', () => {
  const ui = harness();
  ui.state.authMode = 'register';
  ui.state.capabilities = { email_login: true, phone_verification: true };
  const html = ui.auth();
  assert.match(html, /name="guardian_email"/);
  assert.doesNotMatch(html, /name="guardian_email"[^>]*required/);
  assert.match(html, /선택/);
  ui.state.authMode = 'login';
  assert.doesNotMatch(ui.auth(), /guardian_email/);
});

test('signup sends the guardian email only when one was typed', async () => {
  const done = () => response({ user: { id: 'u', email: 'walker@example.com', nickname: 'n' }, csrf_token: 'c', capabilities: { email_login: true }, settings: {} });
  const run = async guardian => {
    const ui = harness(url => url === '/api/auth/register' ? done() : response({ trip: null, notifications: [] }));
    ui.state.authMode = 'register';
    ui.state.capabilities = { email_login: true, phone_verification: true };
    ui.state.phoneAuth = { ...ui.state.phoneAuth, step: 'verified', value: '01012345678', proof: 'p' };
    ui.form.fields.guardian_email = { name: 'guardian_email', value: guardian };
    await ui.submitForm();
    return ui.requests.find(r => r.url === '/api/auth/register').body;
  };
  assert.equal((await run('  mom@example.com ')).guardian_email, 'mom@example.com');
  assert.equal('guardian_email' in (await run('   ')), false);
});

test('signup result messages say exactly what happened with the guardian request', () => {
  const ui = harness();
  assert.equal(ui.registerMessage(undefined), '가입했어요. 반가워요!');
  assert.match(ui.registerMessage({ status: 'sent' }), /연결 요청을 보냈어요/);
  const failed = ui.registerMessage({ status: 'failed', message: '귀가ON에 가입된 보호자 이메일인지 확인해 주세요.' });
  assert.match(failed, /^가입했어요/);
  assert.match(failed, /보내지 못했어요/);
  assert.match(failed, /보호자 관리에서 다시 요청/);
  assert.doesNotMatch(failed, /연결 요청을 보냈어요/);
});

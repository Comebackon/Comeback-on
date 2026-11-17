/* Run with: node --test tests/test_sms_ui.cjs. No browser or live API. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../mobile/app.js'), 'utf8').replace(/\ninit\(\);\s*$/, '');

function harness() {
  const context = {
    document: {
      querySelector: selector => ({ '#dialog': { open: false, addEventListener() {} }, '#app': { innerHTML: '' } }[selector] || null),
      querySelectorAll: () => [], addEventListener() {}, activeElement: null,
    },
    window: { scrollTo() {} }, navigator: {}, localStorage: { getItem() { return null; }, setItem() {} },
    URLSearchParams, AbortController,
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    fetch: () => Promise.resolve({ ok: true, json: async () => ({}) }),
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.ui={state,smsContactCard,notificationFeedback};', context);
  return context.ui;
}

test('number card is hidden while the SMS service is not connected', () => {
  const ui = harness();
  ui.state.capabilities = { sms: false };
  assert.equal(ui.smsContactCard(), '');
});

test('guardian can register a number only with explicit consent', () => {
  const ui = harness();
  ui.state.capabilities = { sms: true };
  const html = ui.smsContactCard();
  assert.match(html, /id="sms-contact-form"/);
  assert.match(html, /name="phone"/);
  assert.match(html, /name="consent" required/);
  assert.match(html, /위치는 문자에 담기지 않아요/);
});

test('registered number is shown masked with a delete button', () => {
  const ui = harness();
  ui.state.capabilities = { sms: true };
  ui.state.smsContact = { available: true, phone: '010-****-5678' };
  const html = ui.smsContactCard();
  assert.match(html, /010-\*\*\*\*-5678/);
  assert.match(html, /data-action="sms-contact-delete"/);
  assert.doesNotMatch(html, /id="sms-contact-form"/);
});

test('result messages never claim more than what happened', () => {
  const ui = harness();
  const say = (sms, inApp = 'recorded') => ui.notificationFeedback({ in_app: inApp, sms: { status: sms } }, 'SOS를 보냈어요.');
  assert.match(say('push'), /앱 알림을 보냈어요/);
  assert.doesNotMatch(say('push'), /문자/);
  assert.match(say('sent'), /문자 발송을 요청했어요/);
  assert.doesNotMatch(say('sent'), /도착|전달됐/);
  assert.match(say('partial'), /일부 보호자에게만/);
  assert.match(say('no_phone'), /번호를 등록한 보호자가 없어/);
  assert.match(say('failed'), /문자 발송에 실패했어요/);
  assert.match(say('limited'), /한도/);
  assert.match(say('unavailable'), /문자는 발송되지 않았어요/);
});

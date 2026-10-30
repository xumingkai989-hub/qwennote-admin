const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('app/static/home.js', 'utf8');
function harness(fetch) {
  let click;
  const classes = new Set();
  const attrs = new Map();
  const button = {disabled: false, classList: {add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n))},
    setAttribute: (k, v) => attrs.set(k, v), removeAttribute: k => attrs.delete(k), addEventListener: (_, cb) => click = cb};
  const error = {textContent: 'old error'};
  const feedback = {textContent: ''};
  let reloads = 0;
  let finish;
  let delay;
  vm.runInNewContext(script, {document: {getElementById: id => ({'sync-button': button, 'sync-error': error, 'sync-feedback': feedback})[id]},
    fetch, setTimeout: (callback, ms) => {finish = callback; delay = ms;}, window: {location: {reload: () => reloads++}}});
  return {button, classes, attrs, error, feedback, click: () => click(), finish: () => finish(), get delay() {return delay;}, get reloads() {return reloads;}};
}
test('sync success shows a check for one second, locks button, then reloads current URL', async () => {
  const calls = [];
  const h = harness(async (...args) => {calls.push(args); return {ok: true, json: async () => ({status: 'success'})};});
  const pending = h.click();
  assert.equal(h.button.disabled, true);
  assert.equal(h.attrs.get('aria-busy'), 'true');
  await h.click();
  await new Promise(setImmediate);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/sync');
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.ok(h.classes.has('is-complete'));
  assert.equal(h.delay, 1000);
  assert.equal(h.reloads, 0);
  h.finish(); await pending;
  assert.equal(h.reloads, 1);
  assert.equal(h.button.disabled, false);
});
for (const [name, fetch, message] of [
  ['batch failure', async () => ({ok: true, json: async () => ({status: 'failed', error: '部分录音读取失败，请重试'})}), '部分录音读取失败，请重试'],
  ['HTTP failure', async () => ({ok: false, json: async () => ({detail: '同步正在进行'})}), '同步正在进行'],
  ['network failure', async () => {throw new Error('网络不可用');}, '网络不可用'],
  ['unreadable response', async () => ({ok: false, json: async () => {throw new Error('响应无法读取');}}), '响应无法读取'],
]) {
  test(name + ' displays reason and allows retry without reload', async () => {
    const h = harness(fetch); await h.click();
    assert.equal(h.error.textContent, message);
    assert.equal(h.button.disabled, false);
    assert.equal(h.classes.size, 0);
    assert.equal(h.attrs.has('aria-busy'), false);
    assert.equal(h.reloads, 0);
    await h.click(); assert.equal(h.error.textContent, message);
  });
}

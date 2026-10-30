const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('app/static/home.js', 'utf8');
function harness(fetch, withFull = false) {
  let click;
  const classes = new Set();
  const attrs = new Map();
  const button = {disabled: false, classList: {add: (...names) => names.forEach(n => classes.add(n)), remove: (...names) => names.forEach(n => classes.delete(n))},
    setAttribute: (k, v) => attrs.set(k, v), removeAttribute: k => attrs.delete(k), addEventListener: (_, cb) => click = cb};
  let fullClick;
  const fullButton = {disabled: false, classList: {add() {}, remove() {}},
    setAttribute() {}, removeAttribute() {}, addEventListener: (_, cb) => fullClick = cb};
  const error = {textContent: 'old error'};
  const feedback = {textContent: ''};
  let reloads = 0;
  let finish;
  let delay;
  vm.runInNewContext(script, {document: {getElementById: id => ({'sync-button': button, 'full-sync-button': withFull ? fullButton : null, 'sync-error': error, 'sync-feedback': feedback})[id]},
    fetch, setTimeout: (callback, ms) => {finish = callback; delay = ms;}, window: {location: {reload: () => reloads++}}});
  return {button, fullButton, classes, attrs, error, feedback, click: () => click(), fullClick: () => fullClick(), finish: () => finish(), get delay() {return delay;}, get reloads() {return reloads;}};
}
test('sync success shows a check for one second, locks button, then reloads current URL', async () => {
  const calls = [];
  const h = harness(async (...args) => {calls.push(args); return {ok: true, json: async () => ({status: 'success', fetched: 8, inserted: 2, updated: 1, skipped: 5})};});
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
  assert.equal(h.feedback.textContent, '检查 8 条 · 新增 2 · 更新 1 · 跳过 5');
  assert.equal(h.delay, 1000);
  assert.equal(h.reloads, 0);
  h.finish(); await pending;
  assert.equal(h.reloads, 1);
  assert.equal(h.button.disabled, false);
});
test('full sync posts the explicit URL and locks both buttons until completion', async () => {
  const calls = [];
  const h = harness(async (...args) => {calls.push(args); return {ok: true, json: async () => ({status: 'success', fetched: 4, inserted: 1, updated: 3, skipped: 0})};}, true);
  const pending = h.fullClick();
  assert.equal(h.button.disabled, true);
  assert.equal(h.fullButton.disabled, true);
  await h.click(); await h.fullClick();
  await new Promise(setImmediate);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/sync?full=1');
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].credentials, 'same-origin');
  assert.equal(h.feedback.textContent, '检查 4 条 · 新增 1 · 更新 3 · 跳过 0');
  h.finish(); await pending;
  assert.equal(h.button.disabled, false);
  assert.equal(h.fullButton.disabled, false);
  assert.equal(h.reloads, 1);
});
test('a failed full sync unlocks both buttons for retry', async () => {
  const h = harness(async () => {throw new Error('网络不可用');}, true);
  await h.fullClick();
  assert.equal(h.button.disabled, false);
  assert.equal(h.fullButton.disabled, false);
  assert.equal(h.error.textContent, '网络不可用');
  assert.equal(h.reloads, 0);
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

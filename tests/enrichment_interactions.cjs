const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('app/static/enrichment.js', 'utf8');
function node(props = {}) {
  return {dataset: {}, children: [], textContent: '',
    replaceChildren(...children) {this.children = children;},
    append(child) {this.children.push(child);}, ...props};
}
function harness(responses, runs = [12]) {
  const tags = node(), hotword = node(), retries = node();
  const card = node({dataset: {recordingId: 'record'}, querySelector: s => ({'[data-enrichment-tags]': tags, '[data-enrichment-hotword]': hotword, '[data-enrichment-retries]': retries})[s]});
  const config = node({dataset: {csrf: 'signed'}, querySelector: () => ({textContent: JSON.stringify(runs)})});
  const message = node();
  const timers = [], calls = [];
  let reloads = 0;
  const doc = {
    getElementById: id => ({'enrichment-config': config, 'enrichment-message': message})[id],
    querySelectorAll: s => s === '[data-recording-id]' ? [card] : [],
    addEventListener() {}, createElement: () => node(),
    querySelector: () => null,
  };
  const window = {location: {href: 'https://test/', reload: () => reloads++}};
  vm.runInNewContext(script, {document: doc, window, fetch: async url => {
    calls.push(url); const r = responses.shift();
    if (r instanceof Error) throw r;
    return {ok: true, status: 200, json: async () => r, ...r};
  }, setTimeout: (callback, delay) => {timers.push({callback, delay}); return timers.length;}});
  return {tags, hotword, retries, message, timers, calls, window, get reloads() {return reloads;}, async tick() {await timers.shift().callback();}};
}
const record = (tag, hotword = 'done', extra = {}) => ({recording_id: 'record', tag, hotword, tags: ['述职', '<script>'], hotword_count: 2, ...extra});
test('placeholder becomes actual chips and hotword hint without navigation; terminal run stops', async () => {
  const h = harness([{status: 'running', records: [record('pending', 'running')]}, {status: 'done', records: [record('done')]}]);
  assert.equal(h.timers[0].delay, 2000);
  await h.tick();
  assert.equal(h.tags.children[0].textContent, '标签生成中…');
  assert.match(h.tags.children[0].className, /enrichment-pending/);
  assert.equal(h.hotword.textContent, '热词分析中…');
  await h.tick();
  assert.deepEqual(h.tags.children.map(x => x.textContent), ['述职', '<script>']);
  assert.equal(h.hotword.textContent, '热词已应用 2 处');
  assert.equal(h.reloads, 0);
  assert.equal(h.timers.length, 0);
  assert.equal(h.calls[0], '/api/enrichment/status?run=12');
});
for (const status of [401, 403]) test(`${status} stops polling and gives access feedback`, async () => {
  const h = harness([{status, ok: false}]);
  await h.tick();
  assert.equal(h.timers.length, 0);
  assert.equal(h.message.textContent, status === 401 ? '登录已失效，请重新登录' : '无访问权限');
});
test('missing model shows failure with no retry; ordinary failures offer retry', async () => {
  const h = harness([{status: 'partial', records: [record('failed', 'done', {tag_error: '未配置模型', tag_retryable: false})]}]);
  await h.tick();
  assert.equal(h.tags.children[0].textContent, '未配置模型');
  assert.equal(h.retries.children.length, 0);
  const retry = harness([{status: 'failed', records: [record('failed', 'failed', {tag_retryable: true})]}]);
  await retry.tick();
  assert.equal(retry.retries.children[0].dataset.enrichmentRetry, 'record');
});
test('reopening restores durable pending run IDs and does not poll when none are pending', async () => {
  const reopened = harness([{status: 'done', records: [record('done')]}], [99]);
  await reopened.tick();
  assert.equal(reopened.calls[0], '/api/enrichment/status?run=99');
  assert.equal(harness([], []).timers.length, 0);
});
test('deleted run stops polling; transient network failure is retried', async () => {
  const h = harness([new Error('network'), {status: 404}]);
  await h.tick();
  assert.equal(h.timers.length, 1);
  assert.match(h.message.textContent, /正在重试/);
  await h.tick();
  assert.equal(h.timers.length, 0);
});

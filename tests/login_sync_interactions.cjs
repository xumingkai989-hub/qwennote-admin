const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = fs.readFileSync('app/static/login-sync.js', 'utf8');
function harness(fetch) {
  const timers = [], calls = [];
  let reloads = 0;
  vm.runInNewContext(script, {fetch: (...args) => {calls.push(args); return fetch(...args);},
    setTimeout: (cb, delay) => timers.push({cb, delay}), window: {location: {reload: () => reloads++}}});
  return {timers, calls, get reloads() {return reloads;}, async tick() {await timers.shift().cb();}};
}
test('polls every 3 seconds and reloads exactly once on completion', async () => {
  let running = true;
  const h = harness(async () => ({ok: true, json: async () => ({running})}));
  assert.equal(h.calls.length, 0);
  assert.equal(h.timers[0].delay, 3000);
  await h.tick();
  assert.equal(h.calls[0][0], '/api/sync/status');
  assert.equal(h.calls[0][1].cache, 'no-store');
  assert.equal(h.calls[0][1].credentials, 'same-origin');
  assert.equal(h.reloads, 0);
  assert.equal(h.timers[0].delay, 3000);
  running = false;
  const cb = h.timers[0].cb;
  await h.tick(); await cb();
  assert.equal(h.reloads, 1);
  assert.equal(h.timers.length, 0);
});
test('network and server failures retry without reloading', async () => {
  let attempts = 0;
  const h = harness(async () => {
    if (++attempts === 1) throw Error('offline');
    return {ok: false, status: 503};
  });
  await h.tick(); await h.tick();
  assert.equal(h.reloads, 0);
  assert.equal(h.timers[0].delay, 3000);
});
for (const status of [401, 403]) test(`stops polling on ${status}`, async () => {
  const h = harness(async () => ({ok: false, status}));
  await h.tick();
  assert.equal(h.reloads, 0);
  assert.equal(h.timers.length, 0);
});
test('malformed success payload does not imply completion', async () => {
  const h = harness(async () => ({ok: true, json: async () => ({})}));
  await h.tick();
  assert.equal(h.reloads, 0);
  assert.equal(h.timers.length, 1);
});

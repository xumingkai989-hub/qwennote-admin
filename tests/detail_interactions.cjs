// Run with: node --test tests/detail_interactions.cjs (no browser/dependencies).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/static/detail.js'), 'utf8');

function element(properties = {}) {
  return {
    hidden: false, textContent: '', listeners: {}, classes: new Set(),
    addEventListener(name, handler) { this.listeners[name] = handler; },
    classList: { toggle(name, on) { if (on) this.owner.classes.add(name); else this.owner.classes.delete(name); } },
    scrollIntoView() { this.scrolled = true; },
    focus() { this.focused = true; }, select() { this.selected = true; },
    setSelectionRange(start, end) { this.selection = [start, end]; },
    ...properties,
  };
}

function setup({ failClipboard = false, empty = false } = {}) {
  const copied = [];
  const segments = [['alice', '00:00', '项目开场'], ['alice', '00:26', '安排验收'], ['王小明', '01:20', '<script>纯文本</script>']]
    .map(([speaker, timestamp, text]) => {
      const row = element({ dataset: { speaker, timestamp }, querySelectorAll: () => [{ textContent: text }] });
      row.classList.owner = row;
      return row;
    });
  const ids = Object.fromEntries(['copy-markdown', 'markdown-source', 'copy-status', 'copy-fallback',
    'transcript-toast', 'transcript-copy-fallback', 'transcript-source', 'copy-transcript',
    'transcript-search-status', 'transcript-search'].map(id => [id, element()]));
  ids['copy-markdown'].dataset = { markdownUrl: '/recordings/demo.md' };
  if (!empty) ids['transcript-segments'] = element({ querySelectorAll: () => segments });
  let requests = 0;
  vm.runInNewContext(source, {
    document: { getElementById: id => ids[id] || null },
    navigator: { clipboard: { writeText: async text => {
      if (failClipboard) throw Error('Clipboard denied');
      copied.push(text);
    } } },
    fetch: async () => { requests++; return { ok: true, headers: { get: () => 'text/markdown' }, text: async () => 'original markdown' }; },
    setTimeout: () => 1, clearTimeout: () => {},
  });
  return { ids, segments, copied, requests: () => requests,
    search: value => ids['transcript-search'].listeners.input({ target: { value } }) };
}

test('filter keeps DOM rows, highlights/scrolls matches, clears and makes no requests', () => {
  const app = setup();
  app.search('验收');
  assert.deepEqual(app.segments.map(row => row.hidden), [true, false, true]);
  assert.ok(app.segments[1].classes.has('is-match'));
  assert.ok(app.segments[1].scrolled);
  assert.equal(app.segments.length, 3);
  app.search('不存在');
  assert.ok(app.segments.every(row => row.hidden));
  assert.equal(app.ids['transcript-search-status'].textContent, '没有找到匹配的段落');
  app.search('  ');
  assert.ok(app.segments.every(row => !row.hidden && !row.classes.has('is-match')));
  assert.equal(app.requests(), 0);
});

test('search matches names without case sensitivity and timestamp', () => {
  const app = setup();
  app.search('ALICE');
  assert.deepEqual(app.segments.map(row => row.hidden), [false, false, true]);
  app.search('01:20');
  assert.deepEqual(app.segments.map(row => row.hidden), [true, true, false]);
});

test('click copies raw paragraph with timestamp and shows toast', async () => {
  const app = setup();
  await app.segments[1].listeners.click();
  assert.deepEqual(app.copied, ['00:26 alice\n安排验收']);
  assert.equal(app.ids['transcript-toast'].textContent, '已复制 00:26');
  assert.equal(app.ids['transcript-toast'].hidden, false);
});

test('Enter and Space copy; other keys and repeated keydown do not', async () => {
  const app = setup();
  let prevented = 0;
  for (const key of ['Enter', ' ', 'Tab']) {
    app.segments[0].listeners.keydown({ key, preventDefault() { prevented++; } });
  }
  app.segments[0].listeners.keydown({ key: 'Enter', repeat: true, preventDefault() {} });
  assert.equal(prevented, 2);
  assert.equal(app.copied.length, 2);
});

test('copy full transcript includes filtered-out rows and leaves HTML literal', async () => {
  const app = setup();
  app.search('验收');
  app.ids['copy-transcript'].listeners.click();
  await Promise.resolve();
  assert.equal(app.copied[0], '00:00 alice\n项目开场\n\n00:26 alice\n安排验收\n\n01:20 王小明\n<script>纯文本</script>');
  assert.equal(app.requests(), 0);
});

test('clipboard rejection offers selected text and reports failure honestly', async () => {
  const app = setup({ failClipboard: true });
  await app.segments[1].listeners.click();
  assert.equal(app.ids['transcript-copy-fallback'].hidden, false);
  assert.equal(app.ids['transcript-source'].value, '00:26 alice\n安排验收');
  assert.ok(app.ids['transcript-source'].focused && app.ids['transcript-source'].selected);
  assert.match(app.ids['transcript-toast'].textContent, /自动复制失败/);
});

test('summary/empty view still copies the unchanged Markdown endpoint', async () => {
  const app = setup({ empty: true });
  await app.ids['copy-markdown'].listeners.click();
  assert.deepEqual(app.copied, ['original markdown']);
  assert.equal(app.requests(), 1);
});

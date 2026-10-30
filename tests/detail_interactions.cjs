// Run with: node --test tests/detail_interactions.cjs (no browser/dependencies).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/static/detail.js'), 'utf8');

function element(properties = {}) {
  return {
    hidden: false, textContent: '', listeners: {}, classes: new Set(), attributes: {},
    addEventListener(name, handler) { this.listeners[name] = handler; },
    setAttribute(name, value) { this.attributes[name] = value; },
    click() { return this.listeners.click?.(); },
    classList: { toggle(name, on) { if (on) this.owner.classes.add(name); else this.owner.classes.delete(name); } },
    scrollIntoView() { this.scrolled = true; },
    focus() { this.focused = true; }, select() { this.selected = true; },
    setSelectionRange(start, end) { this.selection = [start, end]; },
    ...properties,
  };
}

function setup({ failClipboard = false, empty = false, failFetch = false } = {}) {
  const copied = [];
  const segments = [['alice', '00:00', '项目开场'], ['alice', '00:26', '安排验收'], ['王小明', '01:20', '<script>纯文本</script>']]
    .map(([speaker, timestamp, text]) => {
      const row = element({ dataset: { speaker, timestamp }, querySelectorAll: () => [{ textContent: text }] });
      row.classList.owner = row;
      return row;
    });
  const ids = Object.fromEntries(['copy-markdown', 'markdown-source', 'copy-status', 'copy-fallback',
    'transcript-toast', 'transcript-copy-fallback', 'transcript-source', 'copy-transcript',
    'transcript-search-status', 'transcript-search', 'export-button', 'export-menu',
    'detail-export', 'download-markdown'].map(id => [id, element()]));
  ids['copy-markdown'].dataset = { markdownUrl: '/recordings/demo.md' };
  ids['copy-markdown'].textContent = '复制 Markdown';
  ids['export-menu'].hidden = true;
  ids['transcript-toast'].hidden = true;
  ids['export-menu'].querySelectorAll = () => [ids['copy-markdown'], ids['download-markdown']];
  ids['detail-export'].contains = target => ['detail-export', 'export-button', 'export-menu',
    'copy-markdown', 'download-markdown'].some(id => ids[id] === target);
  const document = { activeElement: null, listeners: {}, getElementById: id => ids[id] || null,
    addEventListener(name, handler) { this.listeners[name] = handler; } };
  for (const el of Object.values(ids)) el.focus = () => { el.focused = true; document.activeElement = el; };
  const timers = new Map();
  let timerId = 0;
  if (!empty) ids['transcript-segments'] = element({ querySelectorAll: () => segments });
  let requests = 0;
  vm.runInNewContext(source, {
    document,
    navigator: { clipboard: { writeText: async text => {
      if (failClipboard) throw Error('Clipboard denied');
      copied.push(text);
    } } },
    fetch: async () => { requests++; return { ok: !failFetch, headers: { get: () => 'text/markdown' }, text: async () => 'original markdown' }; },
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id),
  });
  return { ids, segments, copied, document, timers, requests: () => requests,
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

function key(target, key, repeat = false) {
  let prevented = false;
  target.listeners.keydown({ key, repeat, preventDefault() { prevented = true; } });
  return prevented;
}

test('export opens, toggles and moves focus with wrapping arrows and Home/End', () => {
  const { ids, document } = setup();
  ids['export-button'].click();
  assert.equal(ids['export-menu'].hidden, false);
  assert.equal(ids['export-button'].attributes['aria-expanded'], 'true');
  assert.equal(document.activeElement, ids['copy-markdown']);
  assert.ok(key(ids['export-menu'], 'ArrowUp'));
  assert.equal(document.activeElement, ids['download-markdown']);
  key(ids['export-menu'], 'ArrowDown');
  assert.equal(document.activeElement, ids['copy-markdown']);
  key(ids['export-menu'], 'End');
  assert.equal(document.activeElement, ids['download-markdown']);
  key(ids['export-menu'], 'Home');
  assert.equal(document.activeElement, ids['copy-markdown']);
  ids['export-button'].click();
  assert.ok(ids['export-menu'].hidden);
  assert.equal(document.activeElement, ids['export-button']);
  key(ids['export-button'], 'ArrowUp');
  assert.equal(document.activeElement, ids['download-markdown']);
});

test('Escape restores trigger focus; outside click and focus dismiss without stealing focus', () => {
  const { ids, document } = setup();
  ids['export-button'].click();
  assert.ok(key(document, 'Escape'));
  assert.ok(ids['export-menu'].hidden);
  assert.equal(ids['export-button'].attributes['aria-expanded'], 'false');
  assert.equal(document.activeElement, ids['export-button']);
  ids['export-button'].click();
  document.listeners.click({ target: ids['copy-markdown'] });
  assert.equal(ids['export-menu'].hidden, false);
  document.listeners.click({ target: {} });
  assert.ok(ids['export-menu'].hidden);
  ids['export-button'].click();
  assert.equal(key(ids['export-menu'], 'Tab'), false);
  ids['transcript-search'].focus();
  document.listeners.focusin({ target: ids['transcript-search'] });
  assert.ok(ids['export-menu'].hidden);
  assert.equal(document.activeElement, ids['transcript-search']);
});

test('download activates with Enter, closes menu and restores focus', () => {
  const { ids, document } = setup();
  key(ids['export-button'], 'ArrowUp');
  assert.ok(key(ids['export-menu'], 'Enter'));
  assert.ok(ids['export-menu'].hidden);
  assert.equal(document.activeElement, ids['export-button']);
});

test('Markdown feedback only changes menu label for 800ms, closes immediately, no toast', async () => {
  const { ids, timers, copied } = setup();
  ids['export-button'].click();
  const copying = ids['copy-markdown'].click();
  assert.ok(ids['export-menu'].hidden);
  await copying;
  assert.deepEqual(copied, ['original markdown']);
  assert.equal(ids['copy-markdown'].textContent, '已复制');
  assert.equal(ids['copy-status'].textContent, '');
  assert.ok(ids['transcript-toast'].hidden);
  const timer = [...timers.values()].find(timer => timer.delay === 800);
  assert.ok(timer);
  timer.fn();
  assert.equal(ids['copy-markdown'].textContent, '复制 Markdown');
});

test('Markdown copy via Enter runs once while pending and ignores repeated keydown', async () => {
  const app = setup();
  app.ids['export-button'].click();
  key(app.ids['export-menu'], 'Enter', true);
  assert.equal(app.requests(), 0);
  key(app.ids['export-menu'], 'Enter');
  await app.ids['copy-markdown'].click();
  assert.equal(app.requests(), 1);
  for (let i = 0; i < 4; i++) await Promise.resolve();
  assert.deepEqual(app.copied, ['original markdown']);
});

test('Markdown clipboard failure selects fallback and keeps the honest menu label', async () => {
  const { ids, timers, document } = setup({ failClipboard: true });
  await ids['copy-markdown'].click();
  assert.ok(ids['export-menu'].hidden);
  assert.equal(ids['copy-fallback'].hidden, false);
  assert.equal(ids['markdown-source'].value, 'original markdown');
  assert.equal(document.activeElement, ids['markdown-source']);
  assert.ok(ids['markdown-source'].selected);
  assert.match(ids['copy-status'].textContent, /自动复制失败/);
  assert.equal(ids['copy-markdown'].textContent, '复制 Markdown');
  assert.equal(timers.size, 0);
});

test('Markdown fetch failure releases the busy state and allows retry', async () => {
  const app = setup({ failFetch: true });
  await app.ids['copy-markdown'].click();
  assert.match(app.ids['copy-status'].textContent, /获取 Markdown 失败/);
  assert.equal(app.ids['copy-markdown'].attributes['aria-disabled'], 'false');
  assert.equal(app.copied.length, 0);
  await app.ids['copy-markdown'].click();
  assert.equal(app.requests(), 2);
});

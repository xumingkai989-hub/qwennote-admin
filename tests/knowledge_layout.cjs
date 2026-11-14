// Run against TestClient-rendered or production-response HTML in LAYOUT_PAGES.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.LAYOUT_PAGES);
async function browserTest(run) {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try { await run(browser); } finally { await browser.close(); }
}
for (const colorScheme of ['light', 'dark']) {
  test(`knowledge list/detail fit mobile and desktop (${colorScheme})`, () => browserTest(async browser => {
    const page = await browser.newPage({colorScheme});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    for (const name of ['knowledge', 'knowledge-detail']) {
      await page.goto(pathToFileURL(path.join(directory, name + '.html')).href);
      assert.equal(await page.locator('.navigation-link[aria-current="page"]').getAttribute('href'), '/knowledge');
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({width, height: 1000});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
        if (name === 'knowledge') {
          const codes = page.locator('.knowledge-command code');
          assert.equal(await codes.count(), 4);
          assert.ok(await codes.evaluateAll(elements => elements.every(el => getComputedStyle(el).whiteSpace === 'pre')));
          assert.ok(await page.locator('.knowledge-copy').evaluateAll(elements => elements.every(el => {
            const rect = el.getBoundingClientRect(); return rect.left >= 0 && rect.right <= window.innerWidth;
          })));
        } else {
          assert.equal(await page.getByRole('heading', {level: 1}).count(), 1);
          assert.ok(await page.getByRole('heading', {name: '当前状态', exact: true}).isVisible());
          assert.ok(await page.getByRole('heading', {name: '会议', exact: true}).isVisible());
          assert.ok(await page.locator('a[href^="/recordings/"]').count() > 0);
        }
        if (colorScheme === 'light' && (width === 1440 || (width === 390 && name === 'knowledge'))) {
          await page.screenshot({path: path.join(directory, `${name}-${width}.png`), fullPage: true});
        }
      }
    }
    assert.deepEqual(errors, []);
  }));
}
test('all commands copy exact text and select text on clipboard failure', () => browserTest(async browser => {
  const page = await browser.newPage();
  await page.goto(pathToFileURL(path.join(directory, 'knowledge.html')).href);
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async text => {window.copiedCommand = text;}}}));
  for (const button of await page.locator('.knowledge-copy').all()) {
    const id = await button.getAttribute('data-copy');
    await button.click();
    assert.equal(await page.evaluate(() => window.copiedCommand), await page.locator('#' + id).textContent());
    assert.equal(await page.locator('.knowledge-copy-status').innerText(), '命令已复制');
  }
  for (const failure of ['rejected', 'missing']) {
    await page.evaluate(mode => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: mode === 'missing' ? undefined : {writeText: async () => {throw new Error('denied');}}}), failure);
    await page.locator('.knowledge-copy').first().click();
    assert.equal(await page.evaluate(() => window.getSelection().toString()), await page.locator('.knowledge-command code').first().textContent());
    assert.equal(await page.locator('.knowledge-copy-status').innerText(), '已选中命令，请手动复制');
  }
}));

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
          const prompt = page.locator('#knowledge-agent-prompt');
          assert.ok(await prompt.isVisible());
          assert.equal(await page.locator('.knowledge-commands').getAttribute('open'), null);
          const dimensions = await prompt.evaluate(el => {
            const style = getComputedStyle(el);
            return {height: el.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
              limit: 22 * parseFloat(style.lineHeight), scrollHeight: el.scrollHeight, clientHeight: el.clientHeight};
          });
          assert.ok(dimensions.height <= dimensions.limit + 1);
          assert.ok(dimensions.scrollHeight > dimensions.clientHeight);
          const primary = page.getByRole('button', {name: '复制提示词', exact: true});
          assert.ok(await primary.isVisible());
          assert.notEqual(await primary.evaluate(el => getComputedStyle(el).backgroundColor), 'rgba(0, 0, 0, 0)');
          await page.locator('.knowledge-agent').scrollIntoViewIfNeeded();
          if (colorScheme === 'light' && [1440, 390].includes(width)) {
            await page.screenshot({path: path.join(directory, `knowledge-agent-${width}.png`)});
          }
          await page.locator('.knowledge-commands summary').click();
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
          const codes = page.locator('.knowledge-command code');
          assert.equal(await codes.count(), 4);
          assert.ok(await codes.evaluateAll(elements => elements.every(el => getComputedStyle(el).whiteSpace === 'pre')));
          assert.ok(await page.locator('.knowledge-copy').evaluateAll(elements => elements.every(el => {
            const rect = el.getBoundingClientRect(); return rect.left >= 0 && rect.right <= window.innerWidth;
          })));
          await page.locator('.knowledge-commands summary').click();
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
test('prompt and all commands copy exact text, restore labels and select text on clipboard failure', () => browserTest(async browser => {
  const page = await browser.newPage();
  await page.goto(pathToFileURL(path.join(directory, 'knowledge.html')).href);
  await page.locator('.knowledge-commands summary').click();
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: async text => {window.copiedCommand = text;}}}));
  for (const button of await page.locator('.knowledge-copy').all()) {
    const id = await button.getAttribute('data-copy');
    await button.click();
    assert.equal(await page.evaluate(() => window.copiedCommand), await page.locator('#' + id).textContent());
    assert.equal(await button.textContent(), '已复制');
    assert.equal(await page.locator('.knowledge-copy-status').innerText(), (await button.getAttribute('data-copy-label')) + '已复制');
  }
  await page.waitForTimeout(2100);
  assert.equal(await page.locator('.knowledge-copy').first().textContent(), '复制提示词');
  assert.deepEqual(await page.locator('.knowledge-command button').allTextContents(), ['复制', '复制', '复制', '复制']);
  // Repeated clicks restart the two-second feedback window.
  await page.clock.install();
  const primary = page.locator('.knowledge-copy').first();
  await primary.click();
  await page.clock.fastForward(1500);
  await primary.click();
  await page.clock.fastForward(600);
  assert.equal(await primary.textContent(), '已复制');
  await page.clock.fastForward(1400);
  assert.equal(await primary.textContent(), '复制提示词');
  for (const failure of ['rejected', 'missing']) {
    await page.evaluate(mode => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: mode === 'missing' ? undefined : {writeText: async () => {throw new Error('denied');}}}), failure);
    for (const button of await page.locator('.knowledge-copy').all()) {
      const id = await button.getAttribute('data-copy');
      const label = await button.getAttribute('data-copy-label');
      await button.click();
      assert.equal(await page.evaluate(() => window.getSelection().toString()), await page.locator('#' + id).textContent());
      assert.equal(await page.locator('.knowledge-copy-status').innerText(), '已选中' + label + '，请手动复制');
      assert.equal(await button.textContent(), label === '提示词' ? '复制提示词' : '复制');
    }
  }
}));

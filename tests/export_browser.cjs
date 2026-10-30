// Run against tests/recording_edit_preview.py; all external requests are blocked.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.EDIT_PREVIEW_URL || 'http://127.0.0.1:8767';
const out = path.resolve(process.env.EXPORT_ARTIFACTS || '../qwennote-admin-artifacts/export-13-20261030');

test('split exports download correct files and keep the menu usable across desktop/mobile/dark', async () => {
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {}),
    args:['--disable-background-networking']});
  try {
    for (const [name, width, colorScheme, view] of [
      ['desktop',1440,'light','summary'], ['mobile',390,'light','transcript'],
      ['dark',320,'dark','summary']]) {
      const page = await browser.newPage({viewport:{width,height:1000}, colorScheme});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      await page.goto(`${base}/recordings/preview-rich-summary?view=${view}`);
      const items = page.locator('#export-menu [role="menuitem"]');
      assert.deepEqual(await items.allTextContents(), ['导出总结','导出转写原文','导出全部','复制 Markdown']);
      const trigger = page.locator('#export-button');
      await trigger.click();
      assert.equal(await page.locator('#download-summary').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('ArrowDown');
      assert.equal(await page.locator('#download-transcript').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('ArrowDown');
      assert.equal(await page.locator('#download-markdown').evaluate(el => el === document.activeElement), true);
      await page.keyboard.press('Home');
      const menu = await page.locator('#export-menu').boundingBox();
      assert.ok(menu.x >= 0 && menu.x + menu.width <= width && menu.y + menu.height <= 1000);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.screenshot({path:path.join(out, `menu-${name}.png`)});
      await page.keyboard.press('Escape');
      for (const [id, part, suffix] of [
        ['download-summary','summary','-总结'], ['download-transcript','transcript','-转写'],
        ['download-markdown','all','']]) {
        await trigger.click();
        const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#' + id).click()]);
        assert.equal(download.suggestedFilename(), '产品例会：就地编辑与导航验收' + suffix + '.md');
        const body = fs.readFileSync(await download.path(), 'utf8');
        assert.ok(body.startsWith('---\n') && body.includes('duration_min: 25'));
        assert.equal(body.includes('\n## 总结\n'), part !== 'transcript');
        assert.equal(body.includes('\n## 转写原文\n'), part !== 'summary');
        assert.equal(body.includes('{origin:'), false);
        assert.equal(await page.locator('#export-menu').isVisible(), false);
        assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(await trigger.evaluate(el => el === document.activeElement), true);
      }
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});

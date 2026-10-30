// Requires loopback-only tests/recording_edit_preview.py; blocks all external URLs.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.EDIT_PREVIEW_URL || 'http://127.0.0.1:8767';
const out = path.resolve(process.env.EDIT_ARTIFACTS || '../qwennote-admin-artifacts/edit-11-20261030');
const original = '小红直播\n\nAI 助手';
const changed = '小鹿直播\n\nAI Tutor';

test('edit, cancel, save, opt-in suggestions, failures, and revert in four browser configurations', async () => {
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {}),
    args:['--disable-background-networking']});
  const checks = [];
  try {
    for (const [suffix, view, width, colorScheme] of [
      ['summary','summary',1440,'light'], ['transcript','transcript',1440,'light'],
      ['mobile','summary',390,'light'], ['dark','transcript',320,'dark']]) {
      const page = await browser.newPage({viewport:{width,height:1000}, colorScheme});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      const url = `${base}/recordings/preview-${suffix}?view=${view}`;
      let bulkRequests = 0;
      let editRequests = 0;
      page.on('request', request => {
        if (request.url().endsWith('/hotwords/bulk')) bulkRequests++;
        if (request.url().endsWith('/edit')) editRequests++;
      });
      await page.goto(url);
      assert.equal(await page.locator('#edit-trace').isVisible(), false);
      await page.locator('#edit-button').click();
      assert.equal(await page.locator('#edit-content').inputValue(), original);
      await page.locator('#edit-content').fill('取消这次修改');
      await page.locator('#cancel-edit').click();
      assert.equal(await page.locator('#edit-content').inputValue(), original);
      assert.equal(editRequests, 0);
      await page.locator('#edit-button').click();
      await page.locator('#edit-content').fill(changed);
      const dimensions = await page.locator('#edit-content').evaluate(el => ({height:el.offsetHeight, lineHeight:getComputedStyle(el).lineHeight, fontSize:getComputedStyle(el).fontSize}));
      assert.ok(dimensions.height >= 360);
      assert.ok(Math.abs(parseFloat(dimensions.lineHeight) / parseFloat(dimensions.fontSize) - 1.6) < .01);
      await page.screenshot({path:path.join(out,`${suffix}-editing.png`),fullPage:true});
      // A failed save leaves the draft and database unchanged.
      await page.route('**/edit', route => route.fulfill({status:500,contentType:'application/json',body:'{}'}));
      await page.locator('#save-edit').click();
      await page.waitForFunction(() => document.getElementById('edit-status').textContent.includes('保存失败'));
      assert.equal(await page.locator('#edit-content').inputValue(), changed);
      await page.unroute('**/edit');
      await page.locator('#save-edit').click();
      await page.locator('#hotword-suggestions').waitFor({state:'visible'});
      assert.equal(await page.locator('#hotword-options input:checked').count(), 2);
      assert.match(await page.locator('#hotword-options').innerText(), /小红直播 → 小鹿直播/);
      await page.locator('#hotword-options input').last().uncheck();
      assert.equal(await page.locator('#save-hotwords').innerText(), '存入选中的 1 条');
      await page.locator('#hotword-options input').first().uncheck();
      assert.equal(await page.locator('#save-hotwords').isDisabled(), true);
      await page.locator('#hotword-options input').first().check();
      await page.locator('#hotword-options input').last().check();
      await page.screenshot({path:path.join(out,`${suffix}-suggestions.png`),fullPage:true});
      assert.equal(bulkRequests, 0);
      // Dismissal persists the override and never writes a hotword.
      await Promise.all([page.waitForNavigation(), page.locator('#dismiss-hotwords').click()]);
      assert.equal(bulkRequests, 0);
      assert.match(await page.locator('#edit-trace').innerText(), /已手动编辑 · \d\d-\d\d \d\d:\d\d/);
      assert.match(await page.locator('#detail-display').innerText(), /小鹿直播/);
      await page.screenshot({path:path.join(out,`${suffix}-trace.png`),fullPage:true});
      await page.locator('#edit-button').click();
      assert.equal(await page.locator('#edit-content').inputValue(), changed);
      await page.locator('#cancel-edit').click();
      await Promise.all([page.waitForNavigation(), page.locator('#revert-button').click()]);
      assert.equal(await page.locator('#edit-trace').isVisible(), false);
      assert.match(await page.locator('#detail-display').innerText(), /小红直播/);
      // Confirm only one selected item. A network error cannot undo the saved body.
      await page.locator('#edit-button').click();
      await page.locator('#edit-content').fill(changed);
      await page.locator('#save-edit').click();
      await page.locator('#hotword-suggestions').waitFor({state:'visible'});
      await page.locator('#hotword-options input').last().uncheck();
      await page.route('**/hotwords/bulk', route => route.fulfill({status:500,contentType:'application/json',body:'{}'}));
      await page.locator('#save-hotwords').click();
      await page.waitForFunction(() => document.getElementById('hotword-status').textContent.includes('正文已保存'));
      assert.match(await page.locator('#saved-preview').innerText(), /小鹿直播/);
      await page.unroute('**/hotwords/bulk');
      const response = page.waitForResponse(r => r.url().endsWith('/hotwords/bulk'));
      await page.locator('#save-hotwords').click();
      const counts = await (await response).json();
      assert.equal(counts.added + counts.existing, 1);
      await page.waitForFunction(() => document.getElementById('dismiss-hotwords').textContent === '完成');
      assert.match(await page.locator('#hotword-status').innerText(), /已存在 \d 条/);
      await Promise.all([page.waitForNavigation(), page.locator('#dismiss-hotwords').click()]);
      assert.match(await page.locator('#detail-display').innerText(), /小鹿直播/);
      // Punctuation-only editing saves without a dialog.
      await page.locator('#edit-button').click();
      await page.locator('#edit-content').fill(changed + '！');
      await Promise.all([page.waitForNavigation(), page.locator('#save-edit').click()]);
      assert.equal(await page.locator('#hotword-suggestions').isVisible(), false);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false);
      assert.deepEqual(errors, []);
      checks.push({width,colorScheme,view,textarea:dimensions,bulkRequests,overflow,errors});
      // Remove confirmed synthetic hotwords before the next independent scenario.
      await page.goto(base + '/hotwords');
      while (await page.locator('form[action$="/delete"] button').count()) {
        await Promise.all([page.waitForNavigation(), page.locator('form[action$="/delete"] button').first().click()]);
      }
      await page.goto(url);
      await Promise.all([page.waitForNavigation(), page.locator('#revert-button').click()]);
      await page.close();
    }
    fs.writeFileSync(path.join(out,'browser-checks.json'), JSON.stringify(checks,null,2));
  } finally { await browser.close(); }
});

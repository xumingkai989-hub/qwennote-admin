// Uses the existing loopback-only synthetic preview; external requests are blocked.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.EDIT_PREVIEW_URL || 'http://127.0.0.1:8767';
const out = path.resolve(process.env.SETTINGS_ARTIFACTS || '../qwennote-admin-artifacts/settings-14-20261030');

test('settings save, validation, CSRF isolation and responsive screenshots', async () => {
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {}),
    args:['--disable-background-networking']});
  try {
    for (const [name,width,colorScheme] of [['desktop',1440,'light'],['mobile',390,'light'],['dark',320,'dark']]) {
      const page = await browser.newPage({viewport:{width,height:1100},colorScheme});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      await page.goto(base + '/settings');
      assert.equal(await page.locator('legend').allTextContents().then(x=>x.join('/')), '同步/同步策略/标签/数据保留');
      for (const key of ['sync_enabled','tag_enabled']) await page.locator('#' + key).check();
      for (const [key,value] of Object.entries({sync_time:'03:00', sync_interval_hours:'0',backfill_days:'90',tag_time:'05:00',retention_days:'0'})) {
        await page.locator('#' + key).fill(value);
      }
      await Promise.all([page.waitForURL('**/settings?saved=1'),page.getByRole('button',{name:'保存设置'}).click()]);
      assert.match(await page.locator('.settings-status').innerText(), /已保存 · 下次同步 \d{2}-\d{2} \d{2}:\d{2}/);
      assert.equal(await page.locator('#sync_time').inputValue(), '03:00');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth),false);
      for (const input of await page.locator('.settings-row input').all()) {
        assert.ok(await input.getAttribute('aria-describedby'));
        assert.ok(await input.evaluate(el=>el.labels.length > 0));
      }
      await page.screenshot({path:path.join(out,`settings-${name}.png`),fullPage:true});
      await page.locator('#sync_interval_hours').fill('-1');
      assert.equal(await page.locator('#sync_interval_hours').evaluate(el=>el.checkValidity()),false);
      await page.locator('#sync_interval_hours').fill('2');
      // A server-rejected request must leave the saved snapshot intact.
      const invalid = await page.evaluate(async()=>{
        const form = new URLSearchParams(new FormData(document.querySelector('.settings-form')));
        form.set('retention_days','-1');
        const response = await fetch('/settings',{method:'POST',body:form});
        return {status:response.status,body:await response.text()};
      });
      assert.equal(invalid.status,400);
      assert.ok(invalid.body.includes('role="alert"'));
      await page.reload();
      assert.equal(await page.locator('#sync_interval_hours').inputValue(),'0');
      assert.equal(await page.locator('#retention_days').inputValue(),'0');
      // Visiting other administrative pages must not invalidate this form's cookie.
      await page.evaluate(async()=>{await fetch('/admin');await fetch('/hotwords');});
      await page.locator('#sync_enabled').uncheck();
      await Promise.all([page.waitForResponse(r=>r.request().method()==='POST' && r.url()===base+'/settings'),page.getByRole('button',{name:'保存设置'}).click()]);
      await page.waitForLoadState('networkidle');
      assert.match(await page.locator('.settings-status').innerText(),/自动同步已关闭/);
      assert.deepEqual(errors,[]);
      await page.close();
    }
  } finally { await browser.close(); }
});

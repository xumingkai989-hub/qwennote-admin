const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = 'http://127.0.0.1:8768';
const out = path.resolve(process.env.MODELS_ARTIFACTS || '../qwennote-admin-artifacts/models-15-20261030');

test('model management flow and desktop/mobile/dark screenshots', async () => {
  fs.mkdirSync(out,{recursive:true});
  const browser = await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH,
    args:['--disable-background-networking']});
  try {
    for (const [name,width,colorScheme] of [['desktop',1440,'light'],['mobile',390,'light'],['dark',320,'dark']]) {
      const page = await browser.newPage({viewport:{width,height:1100},colorScheme});
      const errors = [];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',route=>new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      await page.goto(base + '/models');
      assert.match(await page.locator('.model-current').innerText(),/模型：deepseek-flash · 端点：api.deepseek.com\/anthropic/);
      assert.equal(await page.locator('#api_key').getAttribute('type'),'password');
      assert.ok(!(await page.content()).includes('synthetic-preview-credential'));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth),false);
      assert.match(await page.locator('.model-table').innerText(),/••••abcd/);
      await page.locator('[data-model-id="1"] [data-model-test] button').click();
      await page.waitForFunction(()=>document.querySelector('#model-feedback').textContent==='连接成功');
      await page.screenshot({path:path.join(out,`models-${name}.png`),fullPage:true});
      // Default deletion is rejected after the explicit confirmation.
      page.on('dialog', dialog=>dialog.accept());
      await Promise.all([page.waitForResponse(r=>r.request().method()==='POST' && r.url().endsWith('/models/1/delete')),
        page.locator('[data-model-id="1"] [data-model-delete] button').click()]);
      await page.waitForLoadState('networkidle');
      assert.match(await page.getByRole('alert').innerText(),/先切换默认/);
      await page.locator('#model-name').fill('浏览器验收 ' + name);
      await page.locator('#provider').selectOption('deepseek');
      await page.locator('#base_url').fill('https://synthetic.invalid/anthropic');
      await page.locator('#model_id').fill('synthetic-model');
      await page.locator('#api_key').fill('synthetic-browser-key-efgh');
      await Promise.all([page.waitForResponse(r=>r.request().method()==='POST' && r.url()===base+'/models'),
        page.locator('.model-form button').click()]);
      await page.waitForLoadState('networkidle');
      assert.equal(await page.locator('#api_key').inputValue(),'');
      assert.ok(!(await page.content()).includes('synthetic-browser-key'));
      const row=page.locator('tr').filter({hasText:'浏览器验收 ' + name});
      assert.match(await row.innerText(),/••••efgh/);
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/default')),row.getByRole('button',{name:'设为默认'}).click()]);
      await page.waitForLoadState('networkidle');
      assert.equal(await row.locator('.model-default').innerText(),'默认');
      const first=page.locator('[data-model-id="1"]');
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/toggle')),first.getByRole('button',{name:'停用',exact:true}).click()]);
      await page.waitForLoadState('networkidle');
      assert.match(await first.innerText(),/已停用/);
      // Restore the preview default; delete the non-default model.
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/toggle')),first.getByRole('button',{name:'启用',exact:true}).click()]);
      await page.waitForLoadState('networkidle');
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/default')),first.getByRole('button',{name:'设为默认'}).click()]);
      await page.waitForLoadState('networkidle');
      await Promise.all([page.waitForResponse(r=>r.url().endsWith('/delete')),row.getByRole('button',{name:'删除',exact:true}).click()]);
      await page.waitForLoadState('networkidle');
      assert.equal(await row.count(),0);
      assert.deepEqual(errors,[]);
      await page.close();
    }
  } finally {await browser.close();}
});

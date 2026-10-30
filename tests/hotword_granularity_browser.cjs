// Run tests/hotword_granularity_preview.py first. All external requests are blocked.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = 'http://127.0.0.1:8771';
const out = path.resolve(process.env.WORD_ARTIFACTS || '../qwennote-admin-artifacts/hotword-granularity-18-20261030');

test('short word suggestion, review flag/edit entry, inline errors and standard-term ingestion', async () => {
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {}),
    args:['--disable-background-networking']});
  const checks = [];
  try {
    for (const width of [1440,390]) {
      const page = await browser.newPage({viewport:{width,height:1000}});
      const errors=[]; let dialogs=0;
      page.on('pageerror', e=>errors.push(e.message));
      page.on('dialog', d=>{dialogs++; d.dismiss();});
      await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      await page.goto(base+'/recordings/word-preview');
      if (await page.locator('#edit-trace').isVisible()) {
        await Promise.all([page.waitForNavigation(), page.locator('#revert-button').click()]);
      }
      await page.locator('#edit-button').click();
      await page.locator('#summary-content').fill('本次会议中小鹿直播的转化率');
      const saved = page.waitForResponse(r=>r.url().endsWith('/edit'));
      await page.locator('#edit-button').click();
      const result = await (await saved).json();
      assert.deepEqual(result.suggestions,[{term:'小鹿直播'}]);
      await page.locator('#hotword-suggestions').waitFor({state:'visible'});
      assert.match(await page.locator('#hotword-options').innerText(),/小鹿直播/);
      await page.screenshot({path:path.join(out,`short-suggestion-${width}.png`),fullPage:true});
      await page.goto(base+'/hotwords');
      const review = page.locator('tr').filter({has:page.locator('.hotword-review')});
      assert.equal(await review.count(),1);
      assert.equal(await review.locator('.hotword-review').innerText(),'这条像是整句，建议改成词语本身');
      await page.screenshot({path:path.join(out,`needs-review-${width}.png`),fullPage:true});
      await review.locator('summary').click();
      assert.equal(await review.locator('details').getAttribute('open'),'');
      const form=page.locator('form[action="/hotwords"]');
      await form.locator('[name="term"]').fill('整句，错误');
      await form.locator('button').click();
      assert.equal(await form.locator('[name="term"]').getAttribute('aria-invalid'),'true');
      assert.match(await form.locator('.hotword-field-error').innerText(),/标点/);
      await page.screenshot({path:path.join(out,`inline-error-${width}.png`),fullPage:true});
      await form.locator('[name="term"]').fill(`标准词验收${width}`);
      assert.equal(await form.locator('[name="aliases"]').count(),0);
      await Promise.all([page.waitForNavigation(),form.locator('button').click()]);
      assert.match(await page.locator('tbody').innerText(),new RegExp(`标准词验收${width}`));
      assert.equal(dialogs,0); assert.deepEqual(errors,[]);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      checks.push({width,shortSuggestion:result.suggestions,review:true,inlineErrors:true,dialogs,errors});
      await page.close();
    }
    fs.writeFileSync(path.join(out,'word-browser-checks.json'),JSON.stringify(checks,null,2));
  } finally {await browser.close();}
});

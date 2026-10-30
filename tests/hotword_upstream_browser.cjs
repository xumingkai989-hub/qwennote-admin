// Start tests/hotword_upstream_preview.py. Every upstream call uses an offline fake.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base='http://127.0.0.1:8772';
const out=path.resolve(process.env.UPSTREAM_ARTIFACTS || '../qwennote-admin-artifacts/hotword-upstream-19-20261030');

test('responsive preview/cancel, explicit import selection, and one confirmed term-only batch', async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH,args:['--disable-background-networking']});
 const checks=[];
 try {
  for(const [name,width,colorScheme] of [['desktop',1440,'light'],['mobile',390,'light'],['narrow',320,'light'],['dark',390,'dark']]) {
   const page=await browser.newPage({viewport:{width,height:1000},colorScheme});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
   await page.goto(base+'/hotwords');
   await page.waitForFunction(()=>document.querySelector('#qwen-usage').textContent.includes('已用 4 / 200'));
   assert.equal(await page.locator('td [aria-label="已同步"]').count(),1);
   await page.locator('#qwen-sync').click();
   await page.locator('#qwen-dialog').waitFor({state:'visible'});
   const text=await page.locator('#qwen-plan').innerText();
   assert.match(text,/将推送 2 条/);assert.match(text,/跳过 4 条/);assert.match(text,/推送后占用 6 \/ 200/);
   for(const reason of ['already_synced','invalid_chars','too_long','disabled']) assert.ok(text.includes(reason));
   assert.ok(!text.includes('小鹅直播'));
   const before=await (await page.request.get(base+'/synthetic-calls')).json();
   assert.equal(before.calls.filter(r=>r[0]==='add_personal_hot_word').length,0);
   await page.screenshot({path:path.join(out,`preview-${name}.png`),fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   assert.equal(await page.locator('#qwen-dialog').evaluate(el=>el.scrollWidth>el.clientWidth),false);
   await page.locator('#qwen-close').click();
   await page.locator('#qwen-dialog').waitFor({state:'hidden'});
   assert.deepEqual(errors,[]);checks.push({name,width,errors,previewOnly:true});
   await page.close();
  }
  const page=await browser.newPage();
  await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  await page.goto(base+'/hotwords');
  await page.locator('#qwen-import').click();
  await page.locator('#qwen-dialog').waitFor({state:'visible'});
  assert.match(await page.locator('#qwen-dialog-title').innerText(),/导入候选 2 条/);
  assert.equal(await page.locator('#qwen-plan input:checked').count(),0);
  assert.equal(await page.locator('#qwen-confirm').isDisabled(),true);
  await page.locator('#qwen-plan input[value="上游候选"]').check();
  await page.screenshot({path:path.join(out,'import-selection.png'),fullPage:true});
  await page.locator('#qwen-confirm').click();
  await page.waitForFunction(()=>document.querySelector('#qwen-dialog-feedback').textContent.includes('已添加 1 条'));
  await Promise.all([page.waitForNavigation(),page.locator('#qwen-close').click()]);
  assert.match(await page.locator('tbody').innerText(),/上游候选/);
  assert.ok(!(await page.locator('tbody').innerText()).includes('另一个词'));
  await page.locator('#qwen-sync').click();
  await page.locator('#qwen-dialog').waitFor({state:'visible'});
  const written=page.waitForResponse(r=>r.url().endsWith('/sync-qwennote') && r.request().postData().includes('confirm=1'));
  await page.locator('#qwen-confirm').click();
  assert.equal((await (await written).json()).status,'ok');
  const after=await (await page.request.get(base+'/synthetic-calls')).json();
  const writes=after.calls.filter(r=>r[0]==='add_personal_hot_word');
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0][1].hotWordList.sort(),['AI Tutor','上游候选','小鹿直播'].sort());
  await page.screenshot({path:path.join(out,'confirmed-fake-only.png'),fullPage:true});
  fs.writeFileSync(path.join(out,'browser-checks.json'),JSON.stringify({checks,writes},null,2));
 } finally {await browser.close();}
});

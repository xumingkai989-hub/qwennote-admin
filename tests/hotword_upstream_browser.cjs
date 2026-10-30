// Synthetic localhost only. All upstream requests use httpx.MockTransport.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base='http://127.0.0.1:8772';
const out=path.resolve(process.env.UPSTREAM_ARTIFACTS || '../qwennote-admin-artifacts/hotwords-20-20261030');
test('standard form, immediate single results, repush and serial batch using fake MCP',async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH,args:['--disable-background-networking']});
 const checks=[];
 try {
  for(const [name,width,colorScheme] of [['desktop',1440,'light'],['mobile',390,'light'],['narrow',320,'light'],['dark',390,'dark']]) {
   const page=await browser.newPage({viewport:{width,height:1100},colorScheme});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
   await page.goto(base+'/hotwords');
   await page.waitForFunction(()=>document.querySelector('#qwen-usage').textContent.includes('已用'));
   assert.deepEqual(await page.locator('form[action="/hotwords"] label').allTextContents(),['标准词（必填）','备注（选填）']);
   assert.ok(!(await page.locator('main').innerText()).includes('别名'));
   assert.equal(await page.locator('input[name="aliases"]').count(),0);
   assert.deepEqual(await page.locator('thead th').allTextContents(),['标准词','备注','命中次数','千问','操作']);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(out,`center-${name}.png`),fullPage:true});
   const before=await (await page.request.get(base+'/synthetic-calls')).json();
   assert.equal(before.calls.filter(r=>r[0]==='add_personal_hot_word').length,0);
   assert.deepEqual(errors,[]);checks.push({name,width,errors,automaticPush:false});
   await page.close();
  }
  const page=await browser.newPage({viewport:{width:1440,height:1100}});
  await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  await page.goto(base+'/hotwords');
  const row=page.locator('tbody tr').filter({has:page.getByRole('rowheader',{name:'小鹿直播',exact:true})});
  await row.locator('[data-push]').click();
  await page.waitForFunction(()=>document.querySelector('#qwen-feedback').textContent==='已推送');
  assert.equal(await page.locator('dialog[open]').count(),0);
  assert.equal(await row.locator('[data-push]').isDisabled(),true);
  assert.equal(await row.locator('[data-repush]').isVisible(),true);
  assert.equal(await row.locator('[data-qwen-state]').innerText(),'已推送');
  await page.screenshot({path:path.join(out,'single-success-fake.png'),fullPage:true});
  let calls=await (await page.request.get(base+'/synthetic-calls')).json();
  assert.deepEqual(calls.calls.filter(r=>r[0]==='add_personal_hot_word'),[['add_personal_hot_word',{hotWordList:['小鹿直播']}]]);
  const failure=page.locator('tbody tr').filter({has:page.getByRole('rowheader',{name:'失败词',exact:true})});
  await failure.locator('[data-push]').click();
  await page.waitForFunction(()=>document.querySelector('#qwen-feedback').textContent.includes('千问拒绝'));
  assert.match(await failure.locator('[data-qwen-state]').innerText(),/失败（千问拒绝/);
  await page.screenshot({path:path.join(out,'single-failure-fake.png'),fullPage:true});
  await page.reload();
  assert.match(await failure.locator('[data-qwen-state]').innerText(),/千问拒绝/);
  await page.locator('#qwen-sync').click();
  await page.waitForFunction(()=>document.querySelector('#qwen-feedback').textContent.includes('成功 1 / 失败 4'));
  calls=await (await page.request.get(base+'/synthetic-calls')).json();
  assert.ok(calls.calls.filter(r=>r[0]==='add_personal_hot_word').every(r=>r[1].hotWordList.length===1));
  await page.screenshot({path:path.join(out,'batch-result-fake.png'),fullPage:true});
  fs.writeFileSync(path.join(out,'browser-checks.json'),JSON.stringify({checks,calls:calls.calls},null,2));
 } finally {await browser.close();}
});

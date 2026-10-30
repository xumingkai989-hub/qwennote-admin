const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base='http://127.0.0.1:8769';
const out=path.resolve(process.env.JUDGE_ARTIFACTS || '../qwennote-admin-artifacts/hotword-judge-16-20261030');

test('hotword trace, undo and pending state on desktop/mobile/dark',async()=>{
 fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH,args:['--disable-background-networking']});
 try {
  for(const [name,width,colorScheme,suffix] of [['desktop',1440,'light','summary'],['mobile',390,'light','mobile'],['dark',320,'dark','dark']]) {
   const page=await browser.newPage({viewport:{width,height:1000},colorScheme});
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
   await page.goto(`${base}/recordings/preview-${suffix}?view=summary`);
   assert.equal(await page.locator('#hotword-trace summary').innerText(),'热词已应用 2 处');
   await page.locator('#hotword-trace summary').click();
   assert.match(await page.locator('#hotword-trace').innerText(),/小红直播 → 小鹿直播/);
   assert.match(await page.locator('#hotword-trace').innerText(),/离线审核通过/);
   assert.match(await page.locator('#summary-content').innerText(),/小鹿直播/);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(out,`hotword-applied-${name}.png`),fullPage:true});
   await Promise.all([page.waitForURL(/view=summary/),page.getByRole('button',{name:'撤销总结中的 小红直播',exact:true}).click()]);
   await page.waitForLoadState('networkidle');
   assert.match(await page.locator('#summary-content').innerText(),/小红直播/);
   assert.equal(await page.locator('#hotword-trace summary').innerText(),'热词已应用 1 处');
   await page.locator('#hotword-trace summary').click();
   await page.screenshot({path:path.join(out,`hotword-revoked-${name}.png`),fullPage:true});
   await page.getByRole('button',{name:'撤销转写中的 小红直播',exact:true}).click();
   await page.waitForLoadState('networkidle');
   assert.equal(await page.locator('#hotword-trace').count(),0);
   await page.goto(`${base}/recordings/preview-rich-summary?view=summary`);
   assert.match(await page.locator('.hotword-pending').innerText(),/本条的用词尚未经过热词判断/);
   assert.match(await page.locator('#summary-content').innerText(),/小红直播/);
   assert.deepEqual(errors,[]);
   await page.close();
  }
 } finally {await browser.close();}
});

// Actual browser -> existing FastAPI routes -> temporary SQLite, with fake OAuth only.
const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
let server, browser, base;
const output = process.env.ASSISTANT_SCREENSHOTS;
before(async () => {
  server = spawn(process.env.PYTHON || '.venv/bin/python', ['tests/serve_assistant_fixture.py']);
  base = await new Promise((resolve, reject) => {
    const timer = setTimeout(()=>reject(new Error('Fixture startup timeout')),15000);
    server.once('exit', code=>{clearTimeout(timer);reject(new Error(`Fixture exit ${code}`));});
    server.stderr.on('data', chunk=>process.stderr.write(chunk));
    server.stdout.on('data', chunk=>{const m=chunk.toString().match(/FIXTURE_URL=(\S+)/);if(m){clearTimeout(timer);resolve(m[1]);}});
  });
  browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  if (output) fs.mkdirSync(output,{recursive:true});
});
after(async()=>{await browser?.close();server?.kill();});
async function open(run, width=1440, colorScheme='light') {
  const context = await browser.newContext({viewport:{width,height:1000},colorScheme});
  try {
    const start = await context.request.get(base+'/',{maxRedirects:0});
    const state = new URL(start.headers().location).searchParams.get('state');
    await context.request.get(base+'/auth/callback?code=valid-test-code&state='+encodeURIComponent(state),{maxRedirects:0});
    const page = await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/assistant');
    await run(page);
    assert.deepEqual(errors,[]);
  } finally {await context.close();}
}
const title = p=>p.locator('#category-title-1');
const editor = p=>title(p).locator('input[name="name"]');
async function pencil(p) {await title(p).locator('.category-edit-btn').click();}
async function idle(p) {await p.waitForFunction(()=>document.querySelector('#category-title-1').getAttribute('aria-busy')==='false');}

test('title text never edits; pencil focuses, autosizes and Escape cancels without POST', ()=>open(async p=>{
  let posts=0;p.on('request',r=>{if(r.method()==='POST')posts++;});
  await title(p).locator('.category-name').click();
  assert.equal(await editor(p).isVisible(),false);
  await pencil(p);
  assert.equal(await editor(p).evaluate(e=>e===document.activeElement),true);
  const short=await editor(p).evaluate(e=>e.getBoundingClientRect().width);
  await editor(p).fill('分类名称宽度随内容增加');
  assert.ok(await editor(p).evaluate(e=>e.getBoundingClientRect().width)>short);
  await editor(p).press('Escape');
  assert.equal(await editor(p).isVisible(),false);
  assert.equal(await title(p).locator('.category-name').textContent(),'产品');
  assert.equal(posts,0);
}));
for(const trigger of ['Enter','blur']) test(`${trigger} persists once with CSRF, updates names and recent changes`,()=>open(async p=>{
  const requests=[];p.on('request',r=>{if(r.url().endsWith('/categories/rename')) requests.push(r);});
  await pencil(p);await editor(p).fill('产品规划');
  if(trigger==='Enter') await editor(p).press('Enter');else await p.locator('#tags-heading').click();
  await idle(p);
  assert.equal(requests.length,1);
  assert.ok(new URLSearchParams(requests[0].postData()).get('csrf_token'));
  assert.equal(await title(p).locator('.category-name').textContent(),'产品规划');
  assert.match(await p.locator('.change-list').innerText(),/分类改名 产品 → 产品规划/);
  for(const option of await p.locator('.tag-category-form option[value="1"]').all()) assert.equal(await option.textContent(),'产品规划');
  await p.reload();assert.equal(await title(p).locator('.category-name').textContent(),'产品规划');
  await pencil(p);await editor(p).fill('产品');await editor(p).press('Enter');await idle(p);
}));
test('Chinese IME Enter is not a save; unchanged blur sends nothing',()=>open(async p=>{
  let posts=0;p.on('request',r=>{if(r.method()==='POST')posts++;});
  await pencil(p);
  await editor(p).dispatchEvent('keydown',{key:'Enter',isComposing:true});
  assert.equal(await editor(p).isVisible(),true);
  await p.locator('#tags-heading').click();assert.equal(posts,0);
}));
for(const failure of ['duplicate','blank','network','csrf','busy','login']) test(`${failure} rename restores original and shows error`,()=>open(async p=>{
  if(failure==='network') await p.route('**/categories/rename',r=>r.abort());
  if(failure==='csrf') await title(p).locator('.category-rename [name="csrf_token"]').evaluate(e=>e.value='invalid');
  if(failure==='busy') await p.route('**/categories/rename',r=>r.fulfill({status:409,contentType:'text/html',body:'<p id="assistant-error">标签任务正在运行，请稍后重试。</p>'}));
  if(failure==='login') await p.route('**/categories/rename',r=>r.fulfill({status:200,contentType:'text/html',body:'<h1>登录</h1>'}));
  await pencil(p);await editor(p).fill(failure==='duplicate'?'公司管理':failure==='blank'?'   ':'改名失败');
  await editor(p).press('Enter');
  await p.waitForFunction(()=>document.getElementById('assistant-error').textContent.length>0);
  assert.equal(await title(p).locator('.category-name').textContent(),'产品');
  assert.equal(await editor(p).inputValue(),'产品');
  assert.equal(await editor(p).isVisible(),false);
}));
test('category selection moves a row in usage order, keeps groups expanded, survives reload',()=>open(async p=>{
  const row = p.locator('.tag-row').filter({has:p.locator('.tag-name', {hasText:/^AI Tutor$/})});
  await row.locator('.tag-category-form select').selectOption('2');
  await p.waitForFunction(()=>document.querySelector('#category-body-2 .tag-name').textContent==='AI Tutor');
  assert.equal(await p.locator('#category-title-1 .category-count').innerText(),'（4）');
  assert.equal(await p.locator('#category-title-2 .category-count').innerText(),'（4）');
  assert.equal(await p.locator('.category-toggle').count(),0);
  await p.reload();assert.equal(await p.locator('#category-body-2 .tag-name').first().textContent(),'AI Tutor');
  await row.locator('.tag-category-form select').selectOption('1');
  await p.waitForFunction(()=>document.querySelector('#category-body-1 .tag-name').textContent==='AI Tutor');
}));
for(const color of ['light','dark']) for(const width of [1440,390,320]) test(`${width} ${color}: hierarchy, controls, wrapping and no overflow`,()=>open(async p=>{
  const metrics=await p.evaluate(()=>({scroll:document.documentElement.scrollWidth,
    rows:[...document.querySelectorAll('.tag-row')].map(e=>({padding:getComputedStyle(e).paddingLeft,font:getComputedStyle(e).fontSize,height:e.getBoundingClientRect().height,
      name:e.querySelector('.tag-name').getBoundingClientRect().toJSON(),actions:e.querySelector('.tag-actions').getBoundingClientRect().toJSON()})),
    pencil:[...document.querySelectorAll('.category-edit-btn')].map(e=>({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height})),
    title:getComputedStyle(document.querySelector('.category-title')).fontSize,
    weight:getComputedStyle(document.querySelector('.category-title')).fontWeight,
    gap:document.querySelector('.category-tags').getBoundingClientRect().top-document.querySelector('.category-title').getBoundingClientRect().bottom
  }));
  assert.equal(metrics.scroll,width);assert.equal(metrics.title,'15px');assert.equal(metrics.weight,'600');assert.equal(metrics.gap,8);
  for(const b of metrics.pencil) assert.deepEqual(b,{w:24,h:24});
  for(const row of metrics.rows){assert.equal(row.padding,'28px');assert.equal(row.font,'14px');
    if(width===1440) assert.equal(row.height,44);
    else {assert.ok(row.actions.top>=row.name.bottom);assert.equal(row.actions.left,row.name.left);}
  }
  if(output && color==='light' && width!==320){
    await p.locator('#tags-heading').scrollIntoViewIfNeeded();
    await p.screenshot({path:path.join(output,`${width}-expanded.png`),fullPage:true});
  }
  await pencil(p);
  if(output && color==='light' && width===1440) await p.screenshot({path:path.join(output,'1440-editing.png'),fullPage:true});
  await editor(p).fill('长分类名称'.repeat(8));
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth),width);
  await editor(p).press('Escape');
},width,color));

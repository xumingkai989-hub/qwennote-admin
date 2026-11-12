const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const dir = path.resolve(process.env.LAYOUT_PAGES);
const out = path.resolve(process.env.MOBILE_ARTIFACTS || dir);
fs.mkdirSync(out,{recursive:true});
const url = (name,base=dir) => pathToFileURL(path.join(base,name+'.html')).href;
async function run(fn,width=390) {
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 try {const page=await browser.newPage({viewport:{width,height:844}}); const errors=[]; page.on('pageerror',e=>errors.push(e.message)); await fn(page); assert.deepEqual(errors,[]);} finally {await browser.close();}
}
for(const [result,expected] of [
 [{status:'success',inserted:3,updated:1},'21:40 快速同步 · 新增 3 条'],
 [{status:'success',inserted:0,updated:1},'21:40 快速同步 · 更新 1 条'],
 [{status:'success',inserted:0,updated:0},'21:40 快速同步 · 无变化'],
 [{status:'failed'},'21:40 同步失败 · 查看详情']
]) test('mobile sync result '+expected,()=>run(async p=>{
 await p.goto(url('home')); await p.evaluate(result=>window.mobileDensity.syncResult({...result,started_at:'2026-10-30T21:40:00+08:00'}),result);
 assert.equal(await p.locator('#mobile-sync-result').innerText(),expected);
 assert.equal(await p.locator('#mobile-sync-result').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
 if(result.status==='failed') {await p.locator('#sync-error').evaluate(e=>e.textContent='示例错误'); await p.locator('#mobile-sync-result button').click(); assert.equal(await p.locator('#sync-error').isVisible(),true);}
}));
test('home icon search, secondary sync menu and all visible tags',()=>run(async p=>{
 await p.goto(url('home-tags')); assert.equal(await p.locator('.search-form button svg').isVisible(),true);
 assert.equal(await p.locator('.search-form button').evaluate(e=>getComputedStyle(e).backgroundColor),'rgba(0, 0, 0, 0)');
 assert.equal(await p.locator('#full-sync-button').isVisible(),false);
 await p.locator('.sync-actions > .more-button').click(); assert.equal(await p.locator('#full-sync-button').isVisible(),true); await p.keyboard.press('Escape');
 const cards=await p.locator('.timeline-card').evaluateAll(es=>es.map(e=>{
 const visible=[...e.querySelectorAll('.tag-pill')].filter(t=>t.checkVisibility()); const badge=e.querySelector('.badge').getBoundingClientRect(),time=e.querySelector('.card-created').getBoundingClientRect();
 return {tags:visible.map(t=>t.textContent),aligned:Math.abs((badge.y+badge.height/2)-(time.y+time.height/2))<1};}));
 for(const c of cards){assert.equal(c.tags.length,7);assert.ok(c.tags.every(t=>!/^\+\d+$/.test(t)));assert.ok(c.aligned);}
 await p.screenshot({path:path.join(out,'390-home.png')});
}));
test('detail two-line title, sticky tabs, expanded search focus and speaker sizes',()=>run(async p=>{
 await p.goto(url('transcript'));
 const title=await p.locator('.detail-header h1').evaluate(e=>({clamp:getComputedStyle(e).webkitLineClamp,white:getComputedStyle(e).whiteSpace,overflow:getComputedStyle(e).textOverflow,height:e.getBoundingClientRect().height}));
 assert.equal(title.clamp,'2');assert.equal(title.white,'normal');assert.equal(title.overflow,'clip');assert.ok(title.height<=48);
 assert.equal(await p.locator('#transcript-search').isVisible(),false); await p.locator('#transcript-search-toggle').click(); await p.locator('#transcript-search').evaluate(e=>{if(document.activeElement!==e)throw Error('search not focused');});
 const input=await p.locator('#transcript-search').boundingBox(),tools=await p.locator('.transcript-tools').boundingBox();assert.ok(Math.abs(input.width-tools.width)<1);
 await p.locator('#transcript-search-toggle').click(); await p.evaluate(()=>scrollTo(0,900));
 const header=await p.locator('.detail-header').boundingBox(),tabs=await p.locator('.detail-view-switch').boundingBox();assert.ok(Math.abs(tabs.y-header.height)<1);assert.equal(await p.locator('.detail-view-switch').evaluate(e=>getComputedStyle(e).position),'sticky');
 await p.evaluate(()=>scrollTo(0,0)); await p.screenshot({path:path.join(out,'390-detail.png')});
 await p.setViewportSize({width:360,height:844});
 const sizes=await p.locator('.transcript-segment').first().evaluate(e=>['.speaker-avatar','.speaker-name','.timestamp'].map(s=>{let x=e.querySelector(s),r=x.getBoundingClientRect();return {y:r.y,h:r.height,w:r.width,font:getComputedStyle(x).fontSize};}));
 assert.equal(sizes[0].w,28);assert.equal(sizes[1].font,'14px');assert.equal(sizes[2].font,'12px');assert.ok(Math.max(...sizes.map(x=>x.y))-Math.min(...sizes.map(x=>x.y))<4);
}));
test('assistant single rows, category radio choices, merge second panel and safe menu dismissal',()=>run(async p=>{
 await p.goto(url('assistant')); const row=p.locator('.tag-row').first(); assert.ok((await row.boundingBox()).height<=60);
 await row.locator(':scope > .more-button').click(); const sheet=p.locator('dialog[open]');
 assert.deepEqual(await sheet.locator('.sheet-menu > button').allTextContents(),['修改分类','合并到…','停用']);
 await p.screenshot({path:path.join(out,'390-assistant.png')});
 await sheet.getByRole('button',{name:'修改分类',exact:true}).click();
 assert.equal(await sheet.locator('.sheet-choices button').count(),await row.locator('.tag-category-form select option').count());
 assert.equal(await sheet.locator('.sheet-choices [aria-pressed="true"]').count(),1);
 await sheet.getByRole('button',{name:'返回操作'}).click();await sheet.getByRole('button',{name:'合并到…',exact:true}).click(); assert.equal(await sheet.locator('.sheet-panel select').isVisible(),true);
 await p.keyboard.press('Escape');assert.equal(await p.locator('dialog[open]').count(),0);
 assert.equal(await row.locator(':scope > .more-button').evaluate(e=>e===document.activeElement),true);
 await row.locator(':scope > .more-button').click();await p.mouse.click(5,5);assert.equal(await p.locator('dialog[open]').count(),0);
 await p.locator('.category-title > .more-button').first().click();assert.equal(await p.locator('dialog[open]').getByRole('button',{name:'停用分类',exact:true}).isVisible(),true);
 await p.keyboard.press('Escape');
}));
test('settings stack, sticky save and emulated safe area',()=>run(async p=>{
 const cdp=await p.context().newCDPSession(p);await cdp.send('Emulation.setSafeAreaInsetsOverride',{insets:{bottom:34,top:0,left:0,right:0}});
 await p.goto(url('settings')); const style=await p.locator('.settings-actions').evaluate(e=>({position:getComputedStyle(e).position,bottom:getComputedStyle(e).bottom,padding:getComputedStyle(e).paddingBottom}));assert.deepEqual(style,{position:'sticky',bottom:'0px',padding:'46px'});
 const rows=await p.locator('.settings-row').evaluateAll(es=>es.map(e=>{let a=e.querySelector('label').getBoundingClientRect(),b=e.querySelector('input,select').getBoundingClientRect();return b.y>=a.bottom;}));assert.ok(rows.every(Boolean));
 await p.evaluate(()=>scrollTo(0,650));assert.ok((await p.locator('.settings-actions button').boundingBox()).y<844-34);
 await p.screenshot({path:path.join(out,'390-settings.png')});
}));
for(const name of ['hotwords','models'])test(name+' compact rows, actions and no overflow at 320/360/390',()=>run(async p=>{
 for(const width of [320,360,390]){await p.setViewportSize({width,height:844});await p.goto(url(name));assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth),width);}
 await p.screenshot({path:path.join(out,`390-${name}.png`)});
 await p.locator('main .more-button').first().click();const sheet=p.locator('dialog[open]');
 if(name==='hotwords')assert.equal(await sheet.locator('[data-push]').isVisible(),true);
 else {assert.equal(await sheet.getByRole('button',{name:'设为默认'}).isVisible(),true);assert.equal(await sheet.locator('[data-model-test]').isVisible(),true);}
}));
test('all sheets have 48px controls; desktop resize restores original actions',()=>run(async p=>{
 for(const name of ['home','assistant','hotwords','models']){
 await p.setViewportSize({width:390,height:844});await p.goto(url(name));
 for(const trigger of await p.locator('main .more-button').all()){
 await trigger.click();const heights=await p.locator('dialog[open] :is(button,select,summary,a[href])').evaluateAll(es=>es.filter(e=>e.checkVisibility()).map(e=>e.getBoundingClientRect().height));assert.ok(heights.every(h=>h>=48));
 await p.keyboard.press('Escape');}
 await p.setViewportSize({width:1440,height:1000});await p.waitForFunction(()=>![...document.querySelectorAll('.sheet-content > form,.sheet-content > .tag-actions')].length);assert.equal(await p.locator('.more-button:visible').count(),0);assert.equal(await p.locator('dialog[open]').count(),0);
 if(name==='assistant')assert.equal(await p.locator('.tag-row > .tag-actions .merge-form:visible').count(),await p.locator('.tag-row').count());
 }
}));
if(process.env.BASELINE_PAGES)test('desktop screenshots match baseline in light and dark',()=>run(async p=>{
 const results=[];
 for(const colorScheme of ['light','dark'])for(const name of ['home','home-tags','search','summary','transcript','assistant','settings','hotwords','models']){
 await p.emulateMedia({colorScheme});
 const images=[];
 for(const base of [path.resolve(process.env.BASELINE_PAGES),dir]){
 await p.goto(url(name,base));await p.locator('.settings-status').evaluateAll(es=>es.forEach(e=>e.textContent='下次同步 · 固定测试时间'));await p.locator('.change-list li').evaluateAll(es=>es.forEach(e=>e.textContent=e.textContent.replace(/^.*? · /,'固定测试时间 · ')));
 images.push(await p.screenshot({fullPage:true}));}
 const equal=images[0].equals(images[1]);results.push({name,colorScheme,equal});
 if(!equal){fs.writeFileSync(path.join(out,`${name}-${colorScheme}-before.png`),images[0]);fs.writeFileSync(path.join(out,`${name}-${colorScheme}-after.png`),images[1]);}
 }
 fs.writeFileSync(path.join(out,'desktop-comparison.json'),JSON.stringify(results,null,2));assert.deepEqual(results.filter(x=>!x.equal),[]);
},1440));

test('hotword push disables after success and exposes feedback inside sheet',()=>run(async p=>{
 await p.goto(url('hotwords'));
 await p.evaluate(()=>{window.fetch=async()=>({ok:true,json:async()=>({status:'ok',hotword_id:1,message:'推送成功',currentCount:1,maxCount:100})});});
 await p.locator('main .more-button').first().click();const sheet=p.locator('dialog[open]');await sheet.locator('[data-push]').click();
 await p.waitForFunction(()=>document.querySelector('dialog[open] [data-push]').disabled && document.querySelector('dialog[open] .sheet-status').textContent==='推送成功');
 assert.equal(await p.locator('[data-qwen-state]').innerText(),'已推送');await p.keyboard.press('Escape');await p.locator('main .more-button').first().click();assert.equal(await sheet.locator('[data-push]').isDisabled(),true);
}));
test('model connection test retains form and reports result inside sheet',()=>run(async p=>{
 await p.goto(url('models'));await p.evaluate(()=>{window.fetch=async()=>({ok:true,json:async()=>({ok:true})});});
 await p.locator('main .more-button').first().click();await p.locator('dialog[open] [data-model-test] button').click();
 await p.waitForFunction(()=>document.querySelector('dialog[open] .sheet-status').textContent==='连接成功');
 assert.equal(await p.locator('dialog[open] [data-model-test] button').isDisabled(),false);
}));

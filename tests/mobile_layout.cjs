// Render with tests/render_detail_layout.py; uses existing external Playwright only.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.LAYOUT_PAGES);
const output = path.resolve(process.env.MOBILE_ARTIFACTS || directory);
const pages = ['home','search','summary','transcript','settings','hotwords','models','assistant','admin'];
const header = '.detail-header, .assistant-heading, .settings-page > .list-heading, .models-page > .list-heading';
const url = name => pathToFileURL(path.join(directory, name+'.html')).href;
async function withBrowser(run) {
  const browser = await chromium.launch({headless:true,...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {await run(browser);} finally {await browser.close();}
}
async function audit(page, name, rows) {
  const result = await page.evaluate(() => {
    const elements = [...document.querySelectorAll('button,a[href],input:not([type="hidden"]):not(:disabled),select,textarea,summary,[role="button"]')].filter(e => {
      const r=e.getBoundingClientRect(), s=getComputedStyle(e);
      return e.checkVisibility() && r.width && r.height && s.visibility !== 'hidden' && !e.closest('[inert]') && !e.closest('dialog:not([open])');
    });
    const targets = elements.map((e,i)=>{
      const r=e.getBoundingClientRect();
      return {index:i,tag:e.tagName,id:e.id,class:e.className,label:(e.getAttribute('aria-label') || e.textContent || e.name).trim().slice(0,60),x:r.x,y:r.y,width:r.width,height:r.height,font:parseFloat(getComputedStyle(e).fontSize)};
    });
    const gaps=[];
    for(let i=0;i<elements.length;i++) for(let j=i+1;j<elements.length;j++) {
      if(elements[i].contains(elements[j]) || elements[j].contains(elements[i])) continue;
      // Popup menus overlay the document by design; measure neighbors within the same layer.
      const layer=e=>e.closest('[role="menu"],dialog,.sidebar') || document.body;
      if(layer(elements[i])!==layer(elements[j])) continue;
      const a=targets[i],b=targets[j];
      const dx=Math.max(0,a.x-b.x-b.width,b.x-a.x-a.width);
      const dy=Math.max(0,a.y-b.y-b.height,b.y-a.y-a.height);
      if(Math.hypot(dx,dy)<7.9) gaps.push({a:a.id||a.label,b:b.id||b.label,distance:Math.hypot(dx,dy)});
    }
    return {targets,gaps,scrollWidth:document.documentElement.scrollWidth,width:innerWidth};
  });
  rows.push({name,...result});
  fs.mkdirSync(output,{recursive:true});
  fs.writeFileSync(path.join(output,'touch-targets.json'),JSON.stringify(rows,null,2));
  assert.deepEqual(result.targets.filter(e=>e.width<43.9 || e.height<43.9),[],name+' target sizes');
  assert.deepEqual(result.targets.filter(e=>['INPUT','SELECT','TEXTAREA'].includes(e.tag) && e.font<16),[],name+' input fonts');
  assert.deepEqual(result.gaps,[],name+' adjacent target spacing');
  assert.equal(result.scrollWidth,result.width,name+' page overflow');
}

test('mobile visible controls: 44px hit boxes, 8px spacing and 16px input type',()=>withBrowser(async browser=>{
  const rows=[];
  for(const width of [390,393,719,320]) {
    const page=await browser.newPage({viewport:{width,height:844}});
    for(const name of pages) {
      await page.goto(url(name));
      await audit(page,`${width}/${name}`,rows);
      if(['summary','transcript'].includes(name)) {
        await page.locator('#export-button').click();
        await audit(page,`${width}/${name}/export`,rows);
        await page.keyboard.press('Escape');
        await page.locator('#edit-button').click();
        await page.evaluate(()=>scrollTo(0,0));
        await audit(page,`${width}/${name}/edit`,rows);
        if(name==='summary') {
          assert.deepEqual(await page.locator('.summary-checkbox').evaluate(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,disabled:e.disabled})),{width:16,height:16,disabled:true});
          await page.locator('#cancel-edit').click();
          await page.locator('.markdown-body').evaluate(e=>e.insertAdjacentHTML('beforeend','<p><a href="#first">一</a><a href="#second">二</a></p>'));
          await page.evaluate(()=>scrollTo(0,0));
          await audit(page,`${width}/summary/adjacent-links`,rows);
        }
      }
      if(name==='hotwords') {
        await page.locator('details summary').first().click();
        await page.evaluate(()=>scrollTo(0,0));
        await audit(page,`${width}/hotwords/edit`,rows);
      }
      if(name==='assistant') {
        await page.locator('.category-edit-btn').first().click();
        await page.evaluate(()=>scrollTo(0,0));
        await audit(page,`${width}/${name}/rename`,rows);
      }
      if(name==='home') {
        assert.equal(await page.locator('.mobile-avatar').innerText(),'演');
        assert.equal(await page.locator('.account').isVisible(),false);
        await page.locator('#navigation-open').click();
        assert.equal(await page.locator('.mobile-logout').isVisible(),true);
        await audit(page,`${width}/drawer`,rows);
      }
    }
    await page.close();
  }
  console.log(`AUDIT ${rows.length} page states; ${rows.reduce((n,r)=>n+r.targets.length,0)} visible target observations; zero violations`);
}));

for(const [width,height,safe] of [[390,844,false],[393,852,true],[1440,1000,false]]) {
  test(`${width}: navigation count, sticky header and last content safe area`,()=>withBrowser(async browser=>{
    const page=await browser.newPage({viewport:{width,height}});
    if(safe) {
      const cdp=await page.context().newCDPSession(page);
      await cdp.send('Emulation.setSafeAreaInsetsOverride',{insets:{top:59,bottom:34,left:0,right:0}});
    }
    for(const name of ['summary','transcript','settings','hotwords','models','assistant']) {
      await page.goto(url(name));
      assert.match(await page.locator('meta[name="viewport"]').getAttribute('content'),/viewport-fit=cover/);
      const count=await page.locator('#sidebar, .mobile-navigation, '+header).evaluateAll(es=>es.filter(e=>e.checkVisibility() && e.getBoundingClientRect().height && getComputedStyle(e).visibility!=='hidden' && getComputedStyle(e).display!=='none').length);
      assert.equal(count,width<720?1:2,name+' visible navigation containers');
      const bar=page.locator(header);
      if(width<720) {
        assert.equal(await bar.evaluate(e=>getComputedStyle(e).position),'sticky');
        assert.equal(await bar.evaluate(e=>getComputedStyle(e).top),'0px');
        assert.equal(await bar.evaluate(e=>getComputedStyle(e).borderBottomWidth),'1px');
        if(safe) {
          assert.equal(await bar.evaluate(e=>getComputedStyle(e).paddingTop),'59px');
          assert.equal(await page.locator('body').evaluate(e=>getComputedStyle(e).paddingBottom),'34px');
        }
      } else {
        assert.equal(await page.locator('.mobile-back').isVisible(),false);
      }
      if(name==='transcript') await page.screenshot({path:path.join(output,`${width}-detail-before.png`)});
      await page.evaluate(()=>scrollTo(0,document.documentElement.scrollHeight));
      if(width<720) {
        const bounds=await bar.boundingBox();
        assert.equal(bounds.y,0,name+' sticky after scroll');
        assert.ok(bounds.height<height/3,name+' compact header');
      }
      const last=await page.evaluate(()=>{
        const es=[...document.querySelectorAll('main p,main button,main .transcript-paragraph,main .change-list li, main .model-help')].filter(e=>e.checkVisibility() && e.getBoundingClientRect().height && getComputedStyle(e).visibility!=='hidden' && !e.closest('[hidden]'));
        return Math.max(...es.map(e=>e.getBoundingClientRect().bottom));
      });
      assert.ok(last<=height-(safe?34:0)+1,`${name}: last content ${last} stays above safe area`);
      if(name==='transcript') await page.screenshot({path:path.join(output,`${width}-detail-after.png`)});
    }
  }));
}

const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.BOARD_PAGES);
const output = path.resolve(process.env.BOARD_ARTIFACTS || directory);
async function withPage(width, scheme, action) {
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width,height:1000},colorScheme:scheme});
    const errors=[];
    page.on('pageerror', e=>errors.push(e.message));
    await action(page);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
}
const url = name=>pathToFileURL(path.join(directory,name+'.html')).href;
for (const width of [1440,390]) for (const scheme of ['light','dark']) {
  test(`${width} ${scheme}: weekly levels, geometry, keyboard, no request or overflow`,()=>withPage(width,scheme,async page=>{
    await page.goto(url('allocation'));
    const section=page.locator('.board-allocation');
    assert.match(await section.locator('.board-analysis').textContent(), /本周时间主要投入产品（37.38%）、中台（20.56%）、师资（9.81%）。上周无对比数据。/);
    assert.equal(await section.locator('form input[name="day"]').inputValue(), '2026-11-06');
    const category=section.locator('[data-allocation-panel="by_category"]');
    const tag=section.locator('[data-allocation-panel="by_tag"]');
    const buttons=section.locator('[data-allocation-view]');
    const requests=[];
    page.on('request',r=>requests.push(r.url()));
    assert.equal(await category.evaluate(e=>e.hidden),false);
    assert.equal(await tag.evaluate(e=>e.hidden),true);
    assert.equal(await category.locator('.board-donut-sector').count(),8);
    assert.equal(await tag.locator('.board-donut-sector').count(),8);
    for (const panel of [category,tag]) {
      assert.equal(await panel.locator('.board-legend-percent').evaluateAll(es=>es.reduce((n,e)=>n+parseInt(e.textContent),0)),100);
      assert.match(await panel.locator('.board-legend').textContent(),/未分类/);
      assert.match(await panel.locator('.board-legend').textContent(),/1 场时长未知，未计入/);
      assert.equal(await panel.locator('.board-donut-center strong').textContent(),'21.4');
    }
    const before=await category.boundingBox();
    const screenshot=async name=>{
      await page.evaluate(()=>window.scrollTo(0,0));
      await page.screenshot({path:path.join(output,name),fullPage:true});
    };
    if(scheme==='light') await screenshot(`${width}-category.png`);
    await buttons.nth(1).click();
    assert.equal(await category.evaluate(e=>e.hidden),true);
    assert.equal(await tag.evaluate(e=>e.hidden),false);
    assert.equal(await category.isVisible(),false);
    assert.equal(await buttons.nth(1).getAttribute('aria-pressed'),'true');
    const after=await tag.boundingBox();
    // Compare document position (scrollIntoView may change viewport coordinates).
    const docTop=await tag.evaluate(e=>e.getBoundingClientRect().top+window.scrollY);
    await buttons.nth(0).click();
    assert.equal(await category.evaluate(e=>e.getBoundingClientRect().top+window.scrollY),docTop);
    assert.equal(before.x,after.x);
    await buttons.nth(1).focus();
    await page.keyboard.press('Space');
    assert.equal(await tag.evaluate(e=>e.hidden),false);
    assert.ok(await tag.locator('.board-allocation-name small').count()>0);
    if(width===1440 && scheme==='light') await screenshot('1440-tag.png');
    for(const panel of [tag,category]) {
      await buttons.nth(panel===tag ? 1 : 0).click();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
      const geometry=await panel.evaluate(e=>{
        const ring=e.querySelector('.board-donut').getBoundingClientRect();
        const legend=e.querySelector('.board-legend').getBoundingClientRect();
        return {stacked:legend.top>=ring.bottom,side:legend.left>=ring.right};
      });
      assert.ok(width===390 ? geometry.stacked : geometry.side);
      const controls=await buttons.evaluateAll(es=>es.map(e=>e.getBoundingClientRect().top));
      assert.equal(controls[0],controls[1]);
    }
    const row=category.locator('.board-legend-row').first();
    await row.focus();
    assert.ok(await category.locator('.board-donut-sector').first().evaluate(e=>e.classList.contains('is-active')));
    assert.deepEqual(requests,[]);
  }));
}
test('1440 empty week has message and no SVG',()=>withPage(1440,'light',async page=>{
  await page.goto(url('empty'));
  const section=page.locator('.board-allocation');
  assert.equal(await section.locator('svg').count(),0);
  assert.equal(await section.getByText('本周还没有会议',{exact:true}).count(),1);
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(output,'1440-empty-week.png'),fullPage:true});
}));

const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.BOARD_PAGES);
const output = path.resolve(process.env.BOARD_ARTIFACTS || directory);

for (const width of [1440, 390]) for (const scheme of ['light', 'dark']) {
  test(`${width} ${scheme}: category switching, SVG bands, fixed pixel sizes and responsive resize`, async () => {
    const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    try {
      const page = await browser.newPage({viewport:{width,height:1000},colorScheme:scheme});
      const errors=[];
      page.on('pageerror', e=>errors.push(e.message));
      await page.goto(pathToFileURL(path.join(directory, 'comparison.html')).href);
      const requests=[];
      page.on('request', r=>requests.push(r.url()));
      const section=page.locator('.board-allocation');
      const category=section.locator('[data-allocation-panel="by_category"]');
      const tag=section.locator('[data-allocation-panel="by_tag"]');
      const buttons=section.locator('[data-allocation-view]');
      const state=async selected=>{
        assert.equal(await category.isVisible(),selected===0);
        assert.equal(await tag.isVisible(),selected===1);
        assert.deepEqual(await buttons.evaluateAll(es=>es.map(e=>e.getAttribute('aria-pressed'))),selected===0?['true','false']:['false','true']);
      };
      const screenshot=async (section,name)=>{
        const bounds=await section.evaluate(e=>({y:e.getBoundingClientRect().top+window.scrollY,height:e.getBoundingClientRect().height}));
        await page.screenshot({path:path.join(output,name),fullPage:true,clip:{x:0,y:Math.max(0,bounds.y-16),width,height:bounds.height+32}});
      };
      await state(0);
      if(width===1440 && scheme==='light') await screenshot(section,'1440-switch-before.png');
      await buttons.nth(1).click(); await state(1);
      if(width===1440 && scheme==='light') await screenshot(section,'1440-switch-after.png');
      await buttons.nth(0).click(); await state(0);
      await buttons.nth(1).focus(); await page.keyboard.press('Enter'); await state(1);
      await buttons.nth(0).focus(); await page.keyboard.press('Space'); await state(0);
      const comparison=page.locator('.board-allocation-comparison');
      const inspect=async expectedWidth=>{
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),expectedWidth);
        const facts=await require('./board_comparison_geometry.cjs')(comparison,8);
        assert.ok(Math.abs(facts.plotWidth-facts.viewBoxWidth)<.1);
        assert.equal(facts.rows.reduce((n,r)=>n+r.segments.length,0),31);
        assert.equal(facts.rows.filter(r=>!r.segments.length).length,1);
      };
      await comparison.scrollIntoViewIfNeeded();
      await inspect(width);
      if(scheme==='light') await screenshot(comparison,`${width}-comparison.png`);
      const resized=width===1440?390:1440;
      await page.setViewportSize({width:resized,height:1000});
      await page.waitForFunction(()=>{const e=document.querySelector('.board-comparison-plot');return Math.abs(e.viewBox.baseVal.width-e.getBoundingClientRect().width)<.1;});
      await inspect(resized);
      assert.deepEqual(requests,[]); assert.deepEqual(errors,[]);
    } finally {await browser.close();}
  });
}

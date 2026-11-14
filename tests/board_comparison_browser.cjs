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
        const facts=await comparison.evaluate(section=>{
          const svg=section.querySelector('.board-comparison-plot');
          const rows=[...svg.querySelectorAll('[data-week]')];
          const rects=[...svg.querySelectorAll('rect')];
          const polygons=[...svg.querySelectorAll('polygon')];
          const colors={};
          for(const rect of rects) (colors[rect.dataset.category]??=[]).push(getComputedStyle(rect).fill);
          const connections=polygons.map(p=>{
            const a=rows.findIndex(r=>[...r.querySelectorAll('rect')].some(e=>Number(e.getAttribute('y'))+14===Number(p.dataset.y1)));
            const b=a+1;
            const start=rows[a].querySelector(`rect[data-category="${p.dataset.category}"]`);
            const end=rows[b].querySelector(`rect[data-category="${p.dataset.category}"]`);
            const box=e=>({x:Number(e.getAttribute('x')),end:Number(e.getAttribute('x'))+Number(e.getAttribute('width')),y:Number(e.getAttribute('y'))});
            const r1=box(start),r2=box(end);
            return {points:[...p.points].map(v=>[v.x,v.y]),expected:[[r1.x,r1.y+14],[r1.end,r1.y+14],[r2.end,r2.y],[r2.x,r2.y]],opacity:getComputedStyle(p).fillOpacity,color:getComputedStyle(p).fill,expectedColor:getComputedStyle(start).fill};
          });
          const expectedCount=rows.slice(1).reduce((n,row,i)=>n+[...row.querySelectorAll('rect')].filter(e=>rows[i].querySelector(`rect[data-category="${e.dataset.category}"]`)).length,0);
          return {rowCount:rows.length,rectCount:rects.length,empty:rows.filter(r=>!r.querySelector('rect')).length,colors,connections,expectedCount,
            heights:rects.map(r=>r.getBoundingClientRect().height),radii:rects.map(r=>Number(r.getAttribute('rx'))),
            fontSize:getComputedStyle(section.querySelector('text')).fontSize,
            plotWidth:svg.getBoundingClientRect().width,viewBoxWidth:svg.viewBox.baseVal.width,
            donutColors:[...document.querySelectorAll('[data-allocation-panel="by_category"] .board-legend-row')].map(e=>({name:e.querySelector('.board-allocation-name').textContent.trim(),color:getComputedStyle(e.querySelector('.board-legend-swatch')).backgroundColor})),
            legendColors:[...section.querySelectorAll('li[data-category]')].map(e=>({name:e.dataset.category,color:getComputedStyle(e.querySelector('.board-legend-swatch')).backgroundColor}))};
        });
        assert.equal(facts.rowCount,8); assert.equal(facts.rectCount,31); assert.equal(facts.empty,1);
        assert.equal(facts.connections.length,facts.expectedCount);
        assert.equal(facts.fontSize,'12px');
        assert.ok(Math.abs(facts.plotWidth-facts.viewBoxWidth)<.1);
        assert.ok(facts.heights.every(h=>Math.abs(h-14)<.1));
        assert.ok(facts.radii.every(r=>r===3));
        for(const values of Object.values(facts.colors)) assert.equal(new Set(values).size,1);
        for(const band of facts.connections) {
          assert.equal(band.opacity,'0.18'); assert.equal(band.color,band.expectedColor);
          band.points.forEach((point,i)=>point.forEach((v,j)=>assert.ok(Math.abs(v-band.expected[i][j])<.01)));
        }
        for(const item of facts.donutColors) assert.deepEqual(facts.legendColors.find(x=>x.name===item.name),item);
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

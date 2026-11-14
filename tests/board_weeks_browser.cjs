const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.BOARD_PAGES);
const output = path.resolve(process.env.BOARD_ARTIFACTS || directory);

for (const width of [1440, 390]) for (const javascript of [true, false]) {
  test(`${width}px selectable weeks, JavaScript ${javascript ? 'on' : 'off'}`, async () => {
    const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    try {
      const page = await browser.newPage({viewport:{width,height:1000},javaScriptEnabled:javascript});
      // Serve the real server-rendered fixture selected by the navigation URL.
      await page.route('http://board.test/**', async route => {
        const url = new URL(route.request().url());
        const file = url.pathname.startsWith('/static/') ? path.join(directory,url.pathname) :
          path.join(directory,`comparison-${url.searchParams.get('weeks') || '8'}.html`);
        await route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.css')?'text/css':file.endsWith('.js')?'text/javascript':'text/html'});
      });
      await page.goto('http://board.test/board?day=2026-11-05&weeks=8&filter=a%26b&filter=second');
      const section = page.locator('.board-allocation-comparison');
      const links = section.locator('.board-week-selector a');
      for (const weeks of [4,12,8]) {
        await Promise.all([page.waitForURL(url=>url.searchParams.get('weeks')===String(weeks)), links.filter({hasText:new RegExp(`^${weeks} 周$`)}).click()]);
        const url = new URL(page.url());
        assert.equal(url.searchParams.get('day'),'2026-11-05');
        assert.deepEqual(url.searchParams.getAll('filter'),['a&b','second']);
        assert.equal(await section.locator('h2').textContent(),`近 ${weeks} 周对比`);
        assert.equal(await links.count(),3);
        assert.equal(await section.locator('a[aria-pressed="true"]').textContent(),`${weeks} 周`);
        assert.equal(await section.locator('[data-week]').count(),weeks);
        await require('./board_comparison_geometry.cjs')(section,weeks,javascript);
        const labels=await section.locator('[data-week-label]').count();
        assert.equal(labels,weeks);
        if (javascript && (width===1440 || weeks!==8)) {
          await section.scrollIntoViewIfNeeded();
          await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
          const bounds=await section.evaluate(e=>({y:e.getBoundingClientRect().top,height:e.getBoundingClientRect().height}));
          await page.screenshot({path:path.join(output,`${width}-${weeks}-weeks.png`),clip:{x:0,y:bounds.y-16,width,height:bounds.height+32}});
        }
      }
    } finally {await browser.close();}
  });
}

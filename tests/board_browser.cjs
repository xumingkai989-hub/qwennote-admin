const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.BOARD_PAGES);
const output = path.resolve(process.env.BOARD_ARTIFACTS || directory);
fs.mkdirSync(output, {recursive:true});
async function withPage(width, scheme, action) {
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width,height:1000},colorScheme:scheme});
    const errors = [];
    page.on('pageerror', e=>errors.push(e.message));
    await action(page);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
}
const url = name=>pathToFileURL(path.join(directory, name+'.html')).href;
for (const width of [1440,390]) for (const scheme of ['light','dark']) {
  test(`${width} ${scheme}: live rows, CSS bars, cards, drawer and no overflow`,()=>withPage(width,scheme,async page=>{
    await page.goto(url('data'));
    assert.equal(await page.locator('.board-meeting').count(),6);
    assert.equal(await page.locator('.board-percent').evaluateAll(es=>es.slice(0,6).reduce((n,e)=>n+(parseInt(e.textContent)||0),0)),100);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    assert.equal(await page.locator('.navigation-link[aria-current="page"]').getAttribute('href'),'/board');
    assert.equal(await page.locator('.navigation-link[href="/board"] svg').evaluate(e=>getComputedStyle(e).strokeWidth),'1.7px');
    const cards=await page.locator('.board-card').evaluateAll(es=>es.map(e=>({x:e.offsetLeft,y:e.offsetTop})));
    if(width===390) {
      assert.equal(cards[0].x,cards[1].x); assert.ok(cards[2].y>cards[1].y && cards[1].y>cards[0].y);
      const positions=await page.locator('.board-meeting').first().evaluate(e=>{
        const y=selector=>e.querySelector(selector).getBoundingClientRect().y;
        return {title:y('.board-title'),duration:y('.board-duration'),tags:y('.board-tags'),share:y('.board-share')};
      });
      assert.ok(positions.tags>positions.title && positions.share>positions.duration);
      await page.locator('#navigation-open').click();
      await page.locator('.navigation-link[href="/board"]').waitFor({state:'visible'});
      assert.equal(await page.locator('.navigation-link[href="/board"]').isVisible(),true);
      await page.locator('#navigation-close').click();
    } else assert.equal(cards[0].y,cards[2].y);
    await page.screenshot({path:path.join(output,`${width}-data-${scheme}.png`),fullPage:true});
    await page.goto(url('stress'));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    const tiny=await page.locator('[data-recording-id="tiny"] .board-bar-fill').boundingBox();
    assert.ok(tiny.width>=2);
  }));
}
test('1440 empty state and accessible regeneration form',()=>withPage(1440,'light',async page=>{
  await page.goto(url('empty'));
  assert.equal(await page.getByText('今天没有会议',{exact:true}).count(),1);
  assert.equal(await page.getByText('本周没有会议',{exact:true}).count(),1);
  assert.equal(await page.getByText('分析未生成',{exact:true}).count(),2);
  await page.screenshot({path:path.join(output,'1440-empty.png'),fullPage:true});
  // Prevent actual file navigation after exercising the real submit listener.
  await page.evaluate(()=>document.querySelector('.board-generate').addEventListener('submit',e=>e.preventDefault()));
  await page.getByRole('button',{name:'重新生成日报与周报分析'}).click();
  assert.equal(await page.getByText('生成中…',{exact:true}).count(),1);
  assert.equal(await page.locator('.board-generate button:enabled').count(),0);
}));

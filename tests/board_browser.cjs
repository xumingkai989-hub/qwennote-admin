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
  test(`${width} ${scheme}: charts, cards, legend, drawer and no overflow`,()=>withPage(width,scheme,async page=>{
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
        return {title:y('.board-title'),duration:y('.board-duration'),tags:y('.board-tags'),share:y('.board-percent')};
      });
      assert.ok(positions.tags>positions.title && positions.share>positions.duration);
      await page.locator('#navigation-open').click();
      await page.locator('.navigation-link[href="/board"]').waitFor({state:'visible'});
      assert.equal(await page.locator('.navigation-link[href="/board"]').isVisible(),true);
      await page.locator('#navigation-close').click();
    } else assert.equal(cards[0].y,cards[2].y);
    assert.equal(await page.locator('.board-week-row').count(),7);
    assert.equal(await page.locator('.board-week-row.is-today').getAttribute('data-day'),'2026-11-06');
    assert.equal(await page.locator('.board-week-row.is-future .board-week-numbers').allTextContents().then(xs=>xs.join('')),'——');
    assert.equal(await page.locator('.board-donut-sector').count(),5);
    assert.equal(await page.locator('.board-legend-row.is-uncharted').count(),1);
    assert.equal(await page.locator('.board-legend-percent').evaluateAll(es=>es.reduce((n,e)=>n+(parseInt(e.textContent)||0),0)),100);
    assert.match(await page.locator('.board-donut svg title').first().textContent(),/今日 6 场会议，共 2.7 小时/);
    const layout=await page.locator('.board-donut-layout').evaluate(e=>{
      const ring=e.querySelector('.board-donut').getBoundingClientRect();
      const legend=e.querySelector('.board-legend').getBoundingClientRect();
      return {width:ring.width,height:ring.height,stacked:legend.top>=ring.bottom,side:legend.left>=ring.right};
    });
    assert.equal(layout.width,120); assert.equal(layout.height,120);
    assert.ok(width===390 ? layout.stacked : layout.side);
    const edges=await page.locator('.board-week-row').evaluateAll(es=>es.map(e=>{
      const rect=s=>e.querySelector(s).getBoundingClientRect();
      return [rect('.board-weekday').left,rect('.board-bar').left,rect('.board-week-numbers').right];
    }));
    assert.ok(edges.every(e=>JSON.stringify(e)===JSON.stringify(edges[0])));
    const styles=await page.locator('.board-card').first().evaluate(e=>({
      size:getComputedStyle(e.querySelector('.board-stat-value')).fontSize,
      weight:getComputedStyle(e.querySelector('.board-stat-value')).fontWeight,
      unit:getComputedStyle(e.querySelector('.board-stat-unit')).fontSize,
      border:getComputedStyle(e).borderTopWidth,shadow:getComputedStyle(e).boxShadow
    }));
    assert.deepEqual(styles,{size:'32px',weight:'600',unit:'14px',border:'0px',shadow:'none'});
    const row=page.locator('.board-legend-row[data-chart-key="sector-0"]');
    const sector=page.locator('.board-donut-sector[data-chart-key="sector-0"]');
    await row.hover();
    assert.ok(await sector.evaluate(e=>e.classList.contains('is-active')));
    assert.equal(await sector.evaluate(e=>getComputedStyle(e).filter),'brightness(1.08)');
    await row.locator('a').focus();
    assert.ok(await sector.evaluate(e=>e.classList.contains('is-active')));
    await row.locator('a').blur();
    await page.mouse.move(0,0);
    // First sector starts at twelve o'clock: move to a point inside its stroke.
    const ring=await page.locator('.board-donut').boundingBox();
    await page.mouse.move(ring.x+95,ring.y+25);
    assert.ok(await row.evaluate(e=>e.classList.contains('is-active')));
    await page.mouse.move(0,0);
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(output,`${width}-data-${scheme}.png`),fullPage:true});
    await page.goto(url('stress'));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    assert.equal(await page.locator('.board-donut-sector').count(),6);
    assert.equal(await page.locator('.board-legend-title').getByText('其他',{exact:true}).count(),1);
    assert.equal(await page.locator('.board-legend-percent').evaluateAll(es=>es.reduce((n,e)=>n+(parseInt(e.textContent)||0),0)),100);
    assert.equal(await page.locator('[data-recording-id="tiny"]').count(),1);
    const title=page.locator('.board-legend-title').first();
    assert.ok(await title.evaluate(e=>e.scrollWidth>e.clientWidth));
    assert.equal(await title.evaluate(e=>getComputedStyle(e).textOverflow),'ellipsis');
    await title.focus();
    assert.equal(await title.evaluate(e=>getComputedStyle(e).whiteSpace),'normal');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
    // A tiny meeting may be aggregated; its full title remains in the details.
    assert.ok(await page.locator('[data-recording-id="tiny"] .board-title').textContent());
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

for (const width of [320,720]) test(`${width}: long titles and enlarged text remain readable`,()=>withPage(width,'light',async page=>{
  await page.goto(url('stress'));
  await page.evaluate(()=>document.documentElement.style.fontSize='200%');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
  const overlap=await page.locator('.board-legend-row').evaluateAll(es=>es.some(e=>{
    const range=document.createRange();range.selectNodeContents(e.querySelector('.board-legend-duration'));
    return range.getBoundingClientRect().right > e.querySelector('.board-legend-percent').getBoundingClientRect().left;
  }));
  assert.equal(overlap,false);
  await page.locator('.board-legend-title').first().focus();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);
}));

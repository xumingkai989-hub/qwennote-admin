const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const labels = ['AI Tutor', '中台与数据看板', '模考系统', '导师中心', '述职', '项目规划',
  'LongUnbrokenMeetingTagForResponsiveWrappingVerification'];
const dir = path.resolve(process.env.LAYOUT_PAGES);
const out = path.resolve(process.env.TAGS_ARTIFACTS || dir);
fs.mkdirSync(out, {recursive:true});

for (const colorScheme of ['light', 'dark']) for (const width of [1440,390,320]) {
  test(`all tags wrap without overflow on list/detail at ${width}px ${colorScheme}`, async () => {
    const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
    try {
      const page = await browser.newPage({viewport:{width,height:1000},colorScheme});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('http://**/*', route => route.abort());
      await page.route('https://**/*', route => route.abort());
      for (const name of ['home-tags', 'detail-tags']) {
        await page.goto(pathToFileURL(path.join(dir, name + '.html')).href);
        const result = await page.evaluate(() => ({
          width:innerWidth, scroll:document.documentElement.scrollWidth,
          groups:[...document.querySelectorAll('.recording-tags')].map(tags => {
            const pills=[...tags.querySelectorAll('.tag-pill')], rect=tags.getBoundingClientRect();
            const style=getComputedStyle(pills[0]);
            const title=document.querySelector('.detail-title-row');
            return {texts:pills.map(e=>e.textContent), visible:pills.every(e=>e.checkVisibility()),
              rows:new Set(pills.map(e=>Math.round(e.getBoundingClientRect().top))).size,
              wrap:getComputedStyle(tags).flexWrap,
              belowTitle:!title || rect.top>=title.getBoundingClientRect().bottom+7,
              fits:pills.every(e=>{const r=e.getBoundingClientRect();return r.left>=rect.left-1 && r.right<=rect.right+1 && r.bottom<=rect.bottom+1 && e.scrollWidth<=e.clientWidth+1;}),
              styles:pills.map(e=>{const s=getComputedStyle(e);return [s.whiteSpace,s.textOverflow,s.overflowWrap];}),
              font:style.fontSize,padding:style.padding,radius:style.borderRadius,bg:style.backgroundColor,fg:style.color};
          })
        }));
        assert.equal(result.scroll, width);
        assert.ok(result.groups.length>0);
        if(name==='detail-tags') assert.equal(result.groups.length,1);
        for (const group of result.groups) {
          assert.deepEqual(group.texts,labels);
          assert.ok(group.visible && group.fits && group.belowTitle);
          assert.equal(group.wrap,'wrap');
          if(width<720) assert.ok(group.rows>1);
          for(const style of group.styles) assert.deepEqual(style,['normal','clip','anywhere']);
          assert.equal(group.font,'12px');assert.equal(group.padding,'3px 8px');assert.equal(group.radius,'999px');
          assert.equal(group.bg,colorScheme==='light'?'rgb(237, 242, 255)':'rgb(27, 36, 64)');
          assert.equal(group.fg,colorScheme==='light'?'rgb(44, 74, 154)':'rgb(155, 188, 255)');
        }
        fs.writeFileSync(path.join(out,`${name}-${width}-${colorScheme}.json`),JSON.stringify(result,null,2));
        if(width!==320) await page.screenshot({path:path.join(out,`${name}-${width}-${colorScheme}.png`)});
      }
      await page.goto(pathToFileURL(path.join(dir,'summary.html')).href);
      assert.equal(await page.locator('.detail-tags').count(),0);
      assert.deepEqual(errors,[]);
    } finally {await browser.close();}
  });
}

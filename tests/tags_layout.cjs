const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('tag pills fit 1440/390/320 light and dark; status precedes tags and overflow is accessible', async () => {
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try {
    for (const colorScheme of ['light', 'dark']) {
      for (const width of [1440,390,320]) {
        const page = await browser.newPage({viewport:{width,height:1000},colorScheme});
        await page.route('http://**/*', route => route.abort());
        await page.route('https://**/*', route => route.abort());
        await page.goto(pathToFileURL(path.resolve(process.env.LAYOUT_PAGES, 'home-tags.html')).href);
        const result = await page.evaluate(() => ({
          width:innerWidth, scroll:document.documentElement.scrollWidth,
          cards:[...document.querySelectorAll('.timeline-card')].map(card=>{
            const pill = card.querySelector('.tag-pill'), style = getComputedStyle(pill);
            const tags = card.querySelector('.recording-tags'), status = card.querySelector('.badge');
            const footer=card.querySelector('.card-footer').getBoundingClientRect();
            return {count:tags.children.length, overflow:tags.lastElementChild.textContent,
              title:tags.lastElementChild.title, after:!!(status.compareDocumentPosition(tags)&Node.DOCUMENT_POSITION_FOLLOWING),
              font:style.fontSize,padding:style.padding,radius:style.borderRadius,bg:style.backgroundColor,fg:style.color,
              fits:[...card.querySelectorAll('.badge,.tag-pill,.card-created')].every(el=>{
                const r=el.getBoundingClientRect();return r.left>=footer.left-1 && r.right<=footer.right+1 && r.bottom<=footer.bottom+1;
              })};
          })
        }));
        assert.equal(result.width,result.scroll);
        for(const card of result.cards) {
          assert.equal(card.count,4);assert.equal(card.overflow,'+2');assert.ok(card.title.includes('述职'));
          assert.ok(card.after && card.fits);assert.equal(card.font,'12px');assert.equal(card.padding,'3px 8px');assert.equal(card.radius,'999px');
          assert.equal(card.bg,colorScheme==='light'?'rgb(237, 242, 255)':'rgb(27, 36, 64)');
          assert.equal(card.fg,colorScheme==='light'?'rgb(44, 74, 154)':'rgb(155, 188, 255)');
        }
        await page.close();
      }
    }
  } finally {await browser.close();}
});

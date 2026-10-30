// Render fixtures with render_detail_layout.py, then set LAYOUT_PAGES to that directory.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('home, summary and transcript share the same frame at all six viewports', async () => {
  const directory = path.resolve(process.env.LAYOUT_PAGES);
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  const results = [];
  try {
    for (const width of [1440, 1100, 900, 640, 390, 320]) {
      const page = await browser.newPage({viewport: {width, height: 1000}, deviceScaleFactor: 1, colorScheme: 'light'});
      const row = {viewport: width};
      for (const name of ['home', 'summary', 'transcript']) {
        await page.goto(pathToFileURL(path.join(directory, name + '.html')).href);
        await page.evaluate(() => document.fonts.ready);
        row[name] = await page.evaluate(name => {
          const selector = name === 'home' ? '.recording-list' : '.detail-section';
          const element = document.querySelector(selector);
          const {x, width} = element.getBoundingClientRect();
          const body = document.querySelector(name === 'summary' ? '.markdown-body p' : '.transcript-body');
          return {x, width, scrollWidth: document.documentElement.scrollWidth,
            lineHeight: body ? getComputedStyle(body).lineHeight : null,
            innerWidth: document.querySelector('.transcript-segments')?.getBoundingClientRect().width ?? null};
        }, name);
        assert.equal(row[name].scrollWidth, width, `${name}: no horizontal page overflow at ${width}`);
        if (name !== 'home') {
          assert.equal(row[name].lineHeight, '28px');
          assert.equal(row[name].x, row.home.x, `${name}: x equals home at ${width}`);
          assert.equal(row[name].width, row.home.width, `${name}: width equals home at ${width}`);
          if (width === 1440) assert.ok(row[name].width > 1000, `${name}: desktop section must exceed 1000px`);
        }
        if (name === 'transcript') {
          assert.equal(row[name].innerWidth, row[name].width);
          assert.equal(await page.locator('.transcript-segment + .transcript-segment').first().evaluate(e => getComputedStyle(e).marginTop), '24px');
          assert.equal(await page.locator('.transcript-paragraph + .transcript-paragraph').first().evaluate(e => getComputedStyle(e).marginTop), '24px');
        }
        if (width === 1440 || (width === 390 && name === 'transcript')) {
          await page.screenshot({path: path.join(directory, `${width}-${name}.png`), fullPage: true});
        }
      }
      results.push(row);
      await page.close();
    }
    fs.writeFileSync(path.join(directory, 'layout-measurements.json'), JSON.stringify(results, null, 2));
  } finally {
    await browser.close();
  }
});

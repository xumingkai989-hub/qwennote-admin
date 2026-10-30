// Isolated rendering regression; uses synthetic HTML from render_detail_layout.py.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('home timeline, cards and controls fit desktop/mobile and both color schemes', async () => {
  const directory = path.resolve(process.env.LAYOUT_PAGES);
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  const results = [];
  try {
    for (const colorScheme of ['light', 'dark']) {
      for (const width of [1440, 390, 320]) {
        const page = await browser.newPage({viewport: {width, height: 1000}, colorScheme});
        await page.goto(pathToFileURL(path.join(directory, 'home.html')).href);
        const measurements = await page.evaluate(() => {
          const box = selector => {const {x, y, width, height} = document.querySelector(selector).getBoundingClientRect(); return {x, y, width, height};};
          const groups = [...document.querySelectorAll('.date-group')];
          return {
            viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth,
            button: box('#sync-button'), frame: box('main'), list: box('.timeline'), card: box('.timeline-card'),
            groups: groups.map(group => {
              const line = getComputedStyle(group, '::before');
              const heading = group.querySelector('.date-heading');
              const dot = getComputedStyle(heading, '::before');
              const rect = group.getBoundingClientRect();
              return {line: line.content, width: line.width, color: line.backgroundColor,
                start: rect.y + parseFloat(line.top), end: rect.bottom - parseFloat(line.bottom),
                dotX: heading.getBoundingClientRect().x + parseFloat(dot.left) + 4,
                dotY: heading.getBoundingClientRect().y + parseFloat(dot.top) + 4,
                dotWidth: dot.width, marginTop: getComputedStyle(group).marginTop};
            }),
            gap: getComputedStyle(document.querySelector('.date-cards')).gap,
            cardBackground: getComputedStyle(document.querySelector('.timeline-card')).backgroundColor,
            cardBorder: getComputedStyle(document.querySelector('.timeline-card')).borderColor,
            cardRadius: getComputedStyle(document.querySelector('.timeline-card')).borderRadius,
            titleSize: getComputedStyle(document.querySelector('.recording-title')).fontSize,
            summarySize: getComputedStyle(document.querySelector('.card-summary')).fontSize,
            clamp: getComputedStyle(document.querySelector('.card-summary')).webkitLineClamp,
            padding: getComputedStyle(document.querySelector('.timeline-card-link')).padding,
          };
        });
        assert.equal(measurements.scrollWidth, width);
        assert.equal(measurements.button.width, 44); assert.equal(measurements.button.height, 44);
        assert.equal(measurements.frame.width, Math.min(width, 1120));
        assert.equal(measurements.card.x - measurements.list.x, 20);
        assert.equal(measurements.groups.length, 3);
        for (let i = 0; i < 2; i++) {
          const line = measurements.groups[i];
          assert.equal(line.width, '2px'); assert.equal(line.start, line.dotY);
          assert.equal(line.end, measurements.groups[i + 1].dotY);
          assert.equal(line.dotWidth, '8px');
          assert.equal(line.color, colorScheme === 'dark' ? 'rgb(56, 56, 58)' : 'rgb(229, 229, 234)');
        }
        assert.equal(measurements.groups[2].line, 'none');
        assert.equal(measurements.groups[1].marginTop, '28px');
        assert.equal(measurements.gap, '12px');
        assert.equal(measurements.padding, '16px'); assert.equal(measurements.cardRadius, '12px');
        assert.equal(measurements.titleSize, '17px'); assert.equal(measurements.summarySize, '14px');
        assert.equal(measurements.clamp, '2');
        assert.equal(measurements.cardBackground, colorScheme === 'dark' ? 'rgb(28, 28, 30)' : 'rgb(255, 255, 255)');
        assert.equal(measurements.cardBorder, colorScheme === 'dark' ? 'rgb(56, 56, 58)' : 'rgb(229, 229, 234)');
        await page.locator('.timeline-card-link').first().focus();
        assert.equal(await page.locator('.timeline-card-link').first().evaluate(e => getComputedStyle(e).outlineStyle), 'solid');
        if (width === 1440 || width === 390) await page.screenshot({path: path.join(directory, `${width}-home-${colorScheme}.png`), fullPage: true});
        results.push({colorScheme, ...measurements}); await page.close();
      }
    }
    fs.writeFileSync(path.join(directory, 'home-measurements.json'), JSON.stringify(results, null, 2));
  } finally {await browser.close();}
});

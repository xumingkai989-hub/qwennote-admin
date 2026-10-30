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
            statuses: [...document.querySelectorAll('.timeline-card')].map(card => {
              const badge = card.querySelector('.badge');
              const created = card.querySelector('.card-created');
              const footer = card.querySelector('.card-footer');
              const b = badge.getBoundingClientRect(), c = created.getBoundingClientRect(), f = footer.getBoundingClientRect();
              const style = getComputedStyle(badge);
              return {label: badge.textContent, x: b.x, right: b.right, centerY: b.y + b.height / 2,
                timeX: c.x, timeRight: c.right, timeCenterY: c.y + c.height / 2, footerX: f.x, footerRight: f.right,
                background: style.backgroundColor, color: style.color, fontSize: style.fontSize,
                padding: style.padding, radius: style.borderRadius};
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
        const palette = colorScheme === 'dark' ? {
          '已转录': ['rgb(28, 58, 38)', 'rgb(133, 217, 160)'],
          '仅转写': ['rgb(22, 35, 63)', 'rgb(155, 188, 255)'],
          '仅总结': ['rgb(58, 42, 18)', 'rgb(245, 199, 126)'],
          '未转录': ['rgb(44, 44, 46)', 'rgb(174, 174, 178)'],
        } : {
          '已转录': ['rgb(231, 247, 236)', 'rgb(27, 127, 59)'],
          '仅转写': ['rgb(232, 240, 255)', 'rgb(37, 81, 168)'],
          '仅总结': ['rgb(255, 244, 229)', 'rgb(154, 91, 0)'],
          '未转录': ['rgb(242, 242, 247)', 'rgb(107, 107, 112)'],
        };
        assert.deepEqual(new Set(measurements.statuses.map(s => s.label)), new Set(Object.keys(palette)));
        for (const status of measurements.statuses) {
          assert.deepEqual([status.background, status.color], palette[status.label]);
          assert.equal(status.fontSize, '12px'); assert.equal(status.padding, '3px 8px'); assert.equal(status.radius, '999px');
          assert.equal(status.x, status.footerX); assert.equal(status.timeRight, status.footerRight);
          assert.ok(status.right < status.timeX); assert.equal(status.centerY, status.timeCenterY);
        }

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

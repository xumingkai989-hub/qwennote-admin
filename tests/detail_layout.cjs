// Render fixtures with render_detail_layout.py, then set LAYOUT_PAGES to that directory.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

test('home and padded detail cards share the same frame without content overflow', async () => {
  const directory = path.resolve(process.env.LAYOUT_PAGES);
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  const results = [];
  try {
    for (const width of [1440, 1100, 1024, 900, 720, 719, 640, 390, 320]) {
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
          const style = getComputedStyle(element);
          const firstText = document.querySelector(name === 'summary' ? '.summary-disclaimer' : '.transcript-count');
          const firstBody = document.querySelector(name === 'summary' ? '.markdown-body > :first-child' : '.speaker-name');
          const segment = document.querySelector('.transcript-segment');
          const range = document.createRange();
          if (firstText) range.selectNodeContents(firstText);
          return {x, width, scrollWidth: document.documentElement.scrollWidth,
            textX: firstText ? range.getBoundingClientRect().x : null,
            bodyX: firstBody?.getBoundingClientRect().x ?? null,
            padding: [style.paddingTop, style.paddingRight, style.paddingBottom, style.paddingLeft],
            grid: segment ? getComputedStyle(segment).gridTemplateColumns : null,
            bodyMinWidth: body ? getComputedStyle(body).minWidth : null,
            lineHeight: body ? getComputedStyle(body).lineHeight : null,
            innerWidth: document.querySelector('.transcript-segments')?.getBoundingClientRect().width ?? null};
        }, name);
        assert.equal(row[name].scrollWidth, width, `${name}: no horizontal page overflow at ${width}`);
        if (name !== 'home') {
          assert.equal(row[name].lineHeight, '28px');
          assert.equal(row[name].x, row.home.x, `${name}: x equals home at ${width}`);
          assert.equal(row[name].width, row.home.width, `${name}: width equals home at ${width}`);
          const inset = width < 720 ? 16 : 32;
          assert.deepEqual(row[name].padding, width < 720 ? ['20px', '16px', '28px', '16px'] : ['28px', '32px', '40px', '32px']);
          assert.ok(row[name].textX - row[name].x >= inset);
          assert.ok(row[name].bodyX - row[name].x >= inset);
          if (width === 1440) {assert.equal(row[name].width, 1088); assert.equal(row[name].x, 176);}
        }
        if (name === 'transcript') {
          assert.equal(row[name].innerWidth, row[name].width - (width < 720 ? 32 : 64));
          assert.equal(row[name].bodyMinWidth, '0px');
          if (width >= 900) assert.deepEqual(row[name].grid.split(' ').slice(0, 2), ['36px', '72px']); // 36 + 12 gap + 72 = 120px identity area.
          assert.equal(await page.locator('.transcript-segment + .transcript-segment').first().evaluate(e => getComputedStyle(e).marginTop), '24px');
          assert.equal(await page.locator('.transcript-paragraph + .transcript-paragraph').first().evaluate(e => getComputedStyle(e).marginTop), '24px');
        }
        if (width === 1440 || (width === 390 && name === 'transcript')) {
          await page.screenshot({path: path.join(directory, `${width}-${name}.png`), fullPage: true});
          await page.screenshot({path: path.join(directory, `${width}-${name}-viewport.png`)});
        }
        if (name !== 'home') {
          // Browser-only stress fixture: never writes recording data or changes screenshots.
          row[name].stress = await page.evaluate(name => {
            const word = 'UnbrokenEnglishWord'.repeat(30);
            const url = 'https://example.com/' + 'long-url-path'.repeat(40);
            if (name === 'summary') {
              const markdown = document.querySelector('.markdown-body');
              markdown.insertAdjacentHTML('beforeend', `<p>${word}</p><p><a href="${url}">${url}</a></p><pre><code>${word}\n${url}</code></pre><div class="summary-table"><table><tbody><tr>${Array.from({length: 5}, () => `<td>${word}</td>`).join('')}</tr></tbody></table></div><blockquote><p>${url}</p><ul><li>引用列表</li></ul></blockquote><ul><li>一级<ul><li>二级<ul><li>${word}</li></ul></li></ul></li></ul>`);
            } else {
              document.querySelector('.transcript-paragraph').textContent += '\n' + word + '\n' + url;
              document.querySelector('.speaker-name').textContent = word;
            }
            const card = document.querySelector('.detail-section');
            const bounds = card.getBoundingClientRect(), style = getComputedStyle(card);
            const left = bounds.left + parseFloat(style.paddingLeft), right = bounds.right - parseFloat(style.paddingRight);
            const selectors = name === 'summary' ? '.markdown-body p, .markdown-body pre, .summary-table, .markdown-body table, .markdown-body blockquote, .markdown-body ul, .markdown-body ol, .markdown-body li' : '.transcript-body, .transcript-paragraph, .speaker-name';
            const overflow = [...document.querySelectorAll(selectors)].filter(e => {
              const rect = e.getBoundingClientRect();
              return e.scrollWidth > e.clientWidth + 1 || rect.left < left - 1 || rect.right > right + 1;
            }).map(e => e.tagName + '.' + e.className);
            return {scrollWidth: document.documentElement.scrollWidth, overflow,
              clipping: [card, ...card.querySelectorAll('blockquote, ul, ol')].some(e => ['hidden', 'clip'].includes(getComputedStyle(e).overflowX))};
          }, name);
          assert.equal(row[name].stress.scrollWidth, width);
          assert.deepEqual(row[name].stress.overflow, [], `${name}: content fits padded area at ${width}`);
          assert.equal(row[name].stress.clipping, false);
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

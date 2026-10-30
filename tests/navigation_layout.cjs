// Browser verification against credential-free TestClient-rendered fixtures.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const directory = path.resolve(process.env.LAYOUT_PAGES);
const url = name => pathToFileURL(path.join(directory, `${name}.html`)).href;
async function withBrowser(run) {
  const browser = await chromium.launch({headless:true, ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  try { await run(browser); } finally { await browser.close(); }
}

for (const colorScheme of ['light', 'dark']) {
  test(`desktop navigation expands, collapses, persists and exposes tooltips (${colorScheme})`, () => withBrowser(async browser => {
    const page = await browser.newPage({viewport:{width:1440,height:1000},colorScheme});
    await page.goto(url('home'));
    await page.evaluate(() => localStorage.clear());
    await page.reload();
    const toggle = page.locator('#navigation-toggle');
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#sidebar').evaluate(e => e.getBoundingClientRect().width), 220);
    assert.match(await page.locator('#sidebar-links').innerText(), /会议/);
    assert.equal(await page.locator('.navigation-link[aria-current="page"]').getAttribute('href'), '/');
    const initial = await page.locator('main').evaluate(e => e.getBoundingClientRect().x);
    assert.equal(initial, 270);
    const border = await page.locator('#sidebar').evaluate(e => ({width:getComputedStyle(e).borderRightWidth, shadow:getComputedStyle(e).boxShadow, color:getComputedStyle(e).borderRightColor}));
    assert.equal(border.width, '1px'); assert.equal(border.shadow,'none');
    assert.equal(border.color,colorScheme === 'light' ? 'rgb(229, 229, 234)' : 'rgb(56, 56, 58)');
    await page.screenshot({path:path.join(directory,`1440-expanded-${colorScheme}.png`)});
    await toggle.click();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#sidebar').evaluate(e => e.getBoundingClientRect().width), 64);
    assert.equal((await page.locator('#sidebar').innerText()).trim(), '');
    assert.equal((await page.locator('#sidebar-links').textContent()).trim(), '');
    assert.equal(await page.locator('main').evaluate(e => e.getBoundingClientRect().x), 192);
    const assistant = page.locator('.navigation-link[href="/assistant"]');
    await assistant.hover();
    assert.equal(await assistant.evaluate(e => getComputedStyle(e,'::after').content), '"AI 助手配置"');
    await page.mouse.move(800,500);
    await page.screenshot({path:path.join(directory,`1440-collapsed-${colorScheme}.png`)});
    await page.reload();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    assert.equal((await page.locator('#sidebar').innerText()).trim(), '');
    await toggle.click();
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert.match(await page.locator('#sidebar-links').innerText(), /AI 助手配置/);
  }));

  test(`mobile drawer overlay, Escape, focus and breakpoint (${colorScheme})`, () => withBrowser(async browser => {
    const page = await browser.newPage({viewport:{width:390,height:844},colorScheme});
    await page.goto(url('home'));
    const opener = page.locator('#navigation-open');
    assert.equal(await opener.isVisible(), true);
    assert.equal(await page.locator('#sidebar').isVisible(), false);
    assert.equal(await page.locator('main').evaluate(e => e.getBoundingClientRect().x), 0);
    await opener.click();
    assert.equal(await opener.getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('#sidebar').isVisible(), true);
    assert.equal(await page.locator('#page-content').evaluate(e => e.inert), true);
    assert.equal(await page.locator('#sidebar').getAttribute('aria-modal'), 'true');
    await page.screenshot({path:path.join(directory,`390-drawer-${colorScheme}.png`)});
    await page.locator('#navigation-overlay').click({position:{x:350,y:300}});
    assert.equal(await opener.getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('#page-content').evaluate(e => e.inert), false);
    await opener.click();
    await page.locator('.navigation-link').last().focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('.sidebar-brand').evaluate(e => e === document.activeElement), true);
    await page.keyboard.press('Escape');
    assert.equal(await opener.evaluate(e => e === document.activeElement), true);
    assert.equal(await page.locator('#sidebar').isVisible(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth),390);
    await page.setViewportSize({width:720,height:900});
    assert.equal(await opener.isVisible(), false);
    assert.equal(await page.locator('#sidebar').isVisible(), true);
    assert.equal(await page.locator('#sidebar').evaluate(e => e.getBoundingClientRect().width),220);
    await page.setViewportSize({width:719,height:900});
    assert.equal(await opener.isVisible(), true);
    assert.equal(await page.locator('#sidebar').isVisible(), false);
  }));
}

test('assistant form validates codepoints, clears saved feedback and confirms merges', () => withBrowser(async browser => {
  const page = await browser.newPage({viewport:{width:1440,height:1100},colorScheme:'light'});
  await page.goto(url('assistant'));
  assert.equal(await page.locator('h2').count(),4);
  assert.equal(await page.locator('#instructions').getAttribute('rows'),'8');
  await page.screenshot({path:path.join(directory,'1440-assistant.png'),fullPage:true});
  const input = page.locator('#instructions');
  await input.fill('字'.repeat(2001));
  assert.equal(await page.locator('#instructions-save').isDisabled(),true);
  assert.match(await page.locator('#instructions-error').innerText(),/2000 字/);
  await input.fill('📝'.repeat(2000));
  assert.equal(await page.locator('#instructions-save').isDisabled(),false);
  assert.equal(await page.locator('#instructions-count').innerText(),'2000 / 2000');
  await page.evaluate(() => document.getElementById('instructions-saved').textContent='已保存 · 14:22');
  await input.fill('新的分类规则');
  assert.equal(await page.locator('#instructions-saved').innerText(),'');
  const merge = page.locator('.merge-form').first();
  await merge.locator('select').selectOption({index:1});
  const dismissed = page.waitForEvent('dialog').then(async dialog => {
    assert.match(dialog.message(), /已有记录的标签也会同步改写/);
    await dialog.dismiss();
  });
  await merge.locator('button').click(); await dismissed;
  assert.equal(page.url(),url('assistant'));
  await page.evaluate(() => {
    window.mergePrevented = null;
    document.addEventListener('submit', event => {window.mergePrevented = event.defaultPrevented; event.preventDefault();});
  });
  const confirmed = page.waitForEvent('dialog').then(dialog => dialog.accept());
  await merge.locator('button').click(); await confirmed;
  assert.equal(await page.evaluate(() => window.mergePrevented),false);
  for (const colorScheme of ['light','dark']) {
    await page.emulateMedia({colorScheme});
    for (const width of [1440,720,390,320]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth),width);
    }
  }
}));

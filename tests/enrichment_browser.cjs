const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.ENRICHMENT_URL || 'http://127.0.0.1:8774';
const out = path.resolve(process.env.ENRICHMENT_ARTIFACTS);
test('real pages: sync returns, reopen restores analysis, chips and detail complete without reload', async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const login = await context.request.get(base + '/', {maxRedirects: 0});
    const state = new URL(login.headers().location).searchParams.get('state');
    const auth = await context.request.get(base + '/auth/callback?code=valid-test-code&state=' + encodeURIComponent(state), {maxRedirects: 0});
    assert.equal(auth.status(), 302);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.evaluate(() => {window.__unchanged = 'preserved';});
    const syncResponse = page.waitForResponse(r => r.url() === base + '/sync' && r.request().method() === 'POST');
    await page.locator('#sync-button').click();
    const sync = await (await syncResponse).json();
    const card = page.locator('[data-recording-id="browser-new"]');
    await card.getByText('标签生成中…').waitFor();
    assert.ok(sync.duration_ms < 1000);
    await page.screenshot({path: path.join(out, 'list-pending.png'), fullPage: false});
    await card.locator('.enrichment-pending').click();
    assert.equal(new URL(page.url()).pathname, '/');
    const reopened = await context.newPage();
    await reopened.goto(base);
    await reopened.locator('[data-recording-id="browser-new"]').getByText('标签生成中…').waitFor();
    await reopened.close();
    const detail = await context.newPage();
    detail.on('pageerror', error => errors.push(error.message));
    await detail.goto(base + '/recordings/browser-new');
    await detail.getByText('热词分析中…').waitFor();
    await detail.screenshot({path: path.join(out, 'detail-pending.png')});
    await detail.evaluate(() => {window.__unchanged = 'preserved';});
    // An active editor draft must survive completion; cancel then applies it.
    await detail.locator('#edit-button').click();
    await page.evaluate(() => window.scrollTo(0, 500));
    const beforeScroll = await page.evaluate(() => scrollY);
    const released = Date.now();
    fs.writeFileSync(path.join(out, 'release-ai'), 'release');
    await card.locator('.tag-pill').filter({hasText: 'AI Tutor'}).waitFor({timeout: 15000});
    const labelDelay = Date.now() - released;
    await card.getByText('热词已应用 2 处').waitFor();
    assert.equal(await page.evaluate(() => window.__unchanged), 'preserved');
    assert.equal(await page.evaluate(() => scrollY), beforeScroll);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: path.join(out, 'list-complete.png')});
    assert.equal(await detail.locator('#summary-content').getAttribute('contenteditable'), 'true');
    await detail.locator('#cancel-edit').click();
    await detail.getByText('热词已应用 2 处').waitFor({timeout: 15000});
    assert.match(await detail.locator('#summary-content').innerText(), /AI导师/);
    assert.equal(await detail.evaluate(() => window.__unchanged), 'preserved');
    await detail.screenshot({path: path.join(out, 'detail-complete.png')});
    // Completion updates the editor baseline: cancel must not restore stale text.
    await detail.locator('#edit-button').click();
    await detail.locator('#cancel-edit').click();
    assert.match(await detail.locator('#summary-content').innerText(), /AI导师/);
    const countBefore = (await context.request.get(`${base}/api/enrichment/status?run=${sync.enrichment_run_id}`));
    assert.equal((await countBefore.json()).status, 'done');
    const pollCalls = [];
    page.on('request', request => {if (request.url().includes('/api/enrichment/status')) pollCalls.push(request.url());});
    await page.waitForTimeout(4500);
    assert.equal(pollCalls.length, 0);
    for (const width of [390, 320]) {
      await page.setViewportSize({width, height: 900});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({path: path.join(out, `list-complete-${width}.png`)});
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(out, 'browser-metrics.json'), JSON.stringify({sync_ms: sync.duration_ms,
      fixture_label_delay_ms: labelDelay, placeholders: true, reopen: true, no_reload: true,
      scroll_preserved: true, editor_preserved: true, terminal_poll_stopped: true, errors}, null, 2));
  } finally {await browser.close();}
});

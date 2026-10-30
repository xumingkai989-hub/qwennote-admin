// Requires loopback-only tests/recording_edit_preview.py; blocks all external URLs.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.EDIT_PREVIEW_URL || 'http://127.0.0.1:8767';
const out = path.resolve(process.env.EDIT_ARTIFACTS || '../qwennote-admin-artifacts/ui-12-20261030');
const original = '小红直播\n\nAI 助手';
const changed = '小鹿直播\n\nAI Tutor';

test('edit, cancel, save, opt-in suggestions, failures, and revert in four browser configurations', async () => {
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {}),
    args:['--disable-background-networking']});
  const checks = [];
  try {
    for (const [suffix, view, width, colorScheme] of [
      ['summary','summary',1440,'light'], ['transcript','transcript',1440,'light'],
      ['mobile','summary',390,'light'], ['dark','transcript',320,'dark']]) {
      const page = await browser.newPage({viewport:{width,height:1000}, colorScheme});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      const url = `${base}/recordings/preview-${suffix}?view=${view}`;
      let bulkRequests = 0;
      let editRequests = 0;
      page.on('request', request => {
        if (request.url().endsWith('/hotwords/bulk')) bulkRequests++;
        if (request.url().endsWith('/edit')) editRequests++;
      });
      await page.goto(url);
      const editor = page.locator(view === 'summary' ? '#summary-content' : '.transcript-body');
      assert.equal(await page.locator('#edit-trace').isVisible(), false);
      const before = await page.locator('#detail-display .detail-section').boundingBox();
      await page.locator('#edit-button').click();
      assert.deepEqual(await page.locator('#detail-display .detail-section').boundingBox(), before);
      assert.equal(await page.locator('[contenteditable="true"]').count(), 1);
      assert.equal(await page.locator('textarea').count(), 0);
      assert.equal(await page.locator('dialog[open]').count(), 0);
      assert.equal(await page.locator('#edit-button').getAttribute('aria-label'), '完成');
      assert.equal(await editor.innerText(), original);
      await editor.fill('取消这次修改');
      await editor.press('Escape');
      assert.equal(await editor.innerText(), original);
      assert.equal(editRequests, 0);
      await page.locator('#edit-button').click();
      await editor.fill(changed);
      const dimensions = await editor.evaluate(el => ({height:el.offsetHeight, lineHeight:getComputedStyle(el).lineHeight, fontSize:getComputedStyle(el).fontSize}));
      assert.ok(dimensions.height > 0);
      assert.ok(Math.abs(parseFloat(dimensions.lineHeight) / parseFloat(dimensions.fontSize) - 1.75) < .01);
      await page.screenshot({path:path.join(out,`${suffix}-editing.png`),fullPage:true});
      // A failed save leaves the draft and database unchanged.
      await page.route('**/edit', route => route.fulfill({status:500,contentType:'application/json',body:'{}'}));
      await page.locator('#edit-button').click();
      await page.waitForFunction(() => document.getElementById('edit-status').textContent.includes('保存失败'));
      assert.equal((await editor.innerText()).replace(/\n{3,}/g, '\n\n'), changed);
      await page.unroute('**/edit');
      await page.locator('#edit-button').click();
      await page.locator('#hotword-suggestions').waitFor({state:'visible'});
      assert.equal(await page.locator('#hotword-options input:checked').count(), 2);
      assert.match(await page.locator('#hotword-options').innerText(), /小红直播 → 小鹿直播/);
      await page.locator('#hotword-options input').last().uncheck();
      assert.equal(await page.locator('#save-hotwords').innerText(), '存入选中的 1 条');
      await page.locator('#hotword-options input').first().uncheck();
      assert.equal(await page.locator('#save-hotwords').isDisabled(), true);
      await page.locator('#hotword-options input').first().check();
      await page.locator('#hotword-options input').last().check();
      await page.screenshot({path:path.join(out,`${suffix}-suggestions.png`),fullPage:true});
      assert.equal(bulkRequests, 0);
      // Dismissal persists the override and never writes a hotword.
      await Promise.all([page.waitForNavigation(), page.locator('#dismiss-hotwords').click()]);
      assert.equal(bulkRequests, 0);
      assert.match(await page.locator('#edit-trace').innerText(), /已手动编辑 · \d\d-\d\d \d\d:\d\d/);
      assert.match(await page.locator('#detail-display').innerText(), /小鹿直播/);
      await page.screenshot({path:path.join(out,`${suffix}-trace.png`),fullPage:true});
      await page.locator('#edit-button').click();
      assert.equal((await editor.innerText()).replace(/\n{3,}/g, '\n\n'), changed);
      await page.locator('#cancel-edit').click();
      await Promise.all([page.waitForNavigation(), page.locator('#revert-button').click()]);
      assert.equal(await page.locator('#edit-trace').isVisible(), false);
      assert.match(await page.locator('#detail-display').innerText(), /小红直播/);
      // Confirm only one selected item. A network error cannot undo the saved body.
      await page.locator('#edit-button').click();
      await editor.fill(changed);
      await page.locator('#edit-button').click();
      await page.locator('#hotword-suggestions').waitFor({state:'visible'});
      await page.locator('#hotword-options input').last().uncheck();
      await page.route('**/hotwords/bulk', route => route.fulfill({status:500,contentType:'application/json',body:'{}'}));
      await page.locator('#save-hotwords').click();
      await page.waitForFunction(() => document.getElementById('hotword-status').textContent.includes('正文已保存'));
      assert.match(await page.locator('#detail-display').innerText(), /小鹿直播/);
      await page.unroute('**/hotwords/bulk');
      const response = page.waitForResponse(r => r.url().endsWith('/hotwords/bulk'));
      await page.locator('#save-hotwords').click();
      const counts = await (await response).json();
      assert.equal(counts.added + counts.existing, 1);
      await page.waitForFunction(() => document.getElementById('dismiss-hotwords').textContent === '完成');
      assert.match(await page.locator('#hotword-status').innerText(), /已存在 \d 条/);
      await Promise.all([page.waitForNavigation(), page.locator('#dismiss-hotwords').click()]);
      assert.match(await page.locator('#detail-display').innerText(), /小鹿直播/);
      // Punctuation-only editing saves without a dialog.
      await page.locator('#edit-button').click();
      await editor.fill(changed + '！');
      await Promise.all([page.waitForNavigation(), editor.press('Control+Enter')]);
      assert.equal(await page.locator('#hotword-suggestions').isVisible(), false);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false);
      assert.deepEqual(errors, []);
      checks.push({width,colorScheme,view,contenteditable:dimensions,bulkRequests,overflow,errors});
      // Remove confirmed synthetic hotwords before the next independent scenario.
      await page.goto(base + '/hotwords');
      while (await page.locator('form[action$="/delete"] button').count()) {
        await Promise.all([page.waitForNavigation(), page.locator('form[action$="/delete"] button').first().click()]);
      }
      await page.goto(url);
      await Promise.all([page.waitForNavigation(), page.locator('#revert-button').click()]);
      await page.close();
    }
    fs.writeFileSync(path.join(out,'browser-checks.json'), JSON.stringify(checks,null,2));
  } finally { await browser.close(); }
});

test('rich layout stays fixed, Markdown and timestamps survive edits, mobile scrolling and empty editors work', async () => {
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {})});
  const checks = [];
  try {
    for (const [suffix,view,width,colorScheme] of [
      ['rich-summary','summary',1440,'light'], ['rich-transcript','transcript',1440,'light'],
      ['rich-mobile','transcript',390,'light'], ['rich-dark','summary',320,'dark']]) {
      const page = await browser.newPage({viewport:{width,height:900},colorScheme});
      const errors=[]; page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      const url = `${base}/recordings/preview-${suffix}?view=${view}`;
      await page.goto(url);
      const initialSource=await page.locator('#edit-source').textContent().then(JSON.parse);
      const content=page.locator(view==='summary'?'#summary-content':'#transcript-segments');
      const metrics=()=>page.locator('.detail-section').evaluate(el=>{
        const body=el.querySelector('.markdown-body p, .transcript-body'); const style=getComputedStyle(body);
        return {card:JSON.stringify(el.getBoundingClientRect()),fontSize:style.fontSize,lineHeight:style.lineHeight,
          html:el.querySelector('#summary-content, #transcript-segments').innerHTML, background:getComputedStyle(el).backgroundColor};
      });
      const before=await metrics();
      await page.locator('#edit-button').click();
      const during=await metrics();
      assert.equal(during.card,before.card); assert.equal(during.fontSize,before.fontSize);
      assert.equal(during.lineHeight,before.lineHeight); assert.equal(during.background,before.background);
      assert.equal(await page.locator('textarea, dialog[open]').count(),0);
      assert.equal(await content.getAttribute('contenteditable'),'true');
      assert.match(await page.locator('.detail-section').evaluate(el=>getComputedStyle(el).boxShadow), /inset/);
      if (view==='transcript') {
        const button=await page.locator('#edit-button').boundingBox(); const search=await page.locator('#transcript-search').boundingBox();
        assert.ok(button.x+button.width<=search.x && Math.abs(button.y-search.y)<5);
      }
      // Native text insertion preserves surrounding headings, lists, tables and transcript identity.
      const leaf=page.locator(view==='summary'?'#summary-content > p':'.transcript-paragraph').filter({hasText:'小红直播'}).first();
      await leaf.evaluate(el=>{
        const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT); let node;
        while((node=walker.nextNode())) {
          const start=node.textContent.indexOf('小红直播'); if(start<0)continue;
          const range=document.createRange();range.setStart(node,start);range.setEnd(node,start+4);
          const selection=getSelection();selection.removeAllRanges();selection.addRange(range);break;
        }
      });
      await page.keyboard.insertText('小鹿直播');
      await page.screenshot({path:path.join(out,`${suffix}-editing.png`),fullPage:true});
      if(width===390) {
        await page.setViewportSize({width,height:430});
        const last=page.locator('.transcript-paragraph').last();await last.scrollIntoViewIfNeeded();
        assert.ok((await last.boundingBox()).y<430);
        await page.setViewportSize({width,height:900});
      }
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      const response=page.waitForResponse(r=>r.url().endsWith('/edit'));
      await content.press('Meta+Enter');
      const result=await (await response).json();assert.equal(result.saved,true);
      assert.deepEqual(result.suggestions,[{aliases:'小红直播',term:'小鹿直播'}]);
      await page.locator('#hotword-suggestions').waitFor({state:'visible'});
      await Promise.all([page.waitForNavigation(),page.locator('#dismiss-hotwords').click()]);
      const saved=await page.locator('#edit-source').textContent().then(JSON.parse);
      assert.equal(saved.trim(),initialSource.replace('小红直播','小鹿直播').trim());
      assert.equal(await page.locator('#sidebar-links a[href="/search"]').count(),0);
      assert.deepEqual(await page.locator('#sidebar-links a').evaluateAll(links=>links.map(link=>link.getAttribute('href'))),
        ['/','/hotwords','/assistant','/models','/settings']);
      if(width===1440 && view==='summary')await page.screenshot({path:path.join(out,'navigation.png'),fullPage:true});
      await Promise.all([page.waitForNavigation(),page.locator('#revert-button').click()]);
      // Cancel restores the original markup; delegated copy/search still works afterwards.
      const html=await content.innerHTML();await page.locator('#edit-button').click();
      await leaf.fill('取消的修改');await content.press('Escape');assert.equal(await content.innerHTML(),html);
      if(view==='transcript') {
        await page.locator('#transcript-search').fill('第 12 段');
        assert.equal(await page.locator('.transcript-segment:visible').count(),1);
      }
      assert.deepEqual(errors,[]);checks.push({suffix,width,colorScheme,before,during,errors});await page.close();
    }
    for(const view of ['summary','transcript']) {
      const page=await browser.newPage();await page.goto(`${base}/recordings/preview-empty-${view}?view=${view}`);
      await page.locator('#edit-button').click();const editor=page.locator('[contenteditable="true"]');
      await editor.fill('新增内容');
      await Promise.all([page.waitForNavigation(),editor.press('Control+Enter')]);
      assert.match(await page.locator('#detail-display').innerText(),/新增内容/);
      await Promise.all([page.waitForNavigation(),page.locator('#revert-button').click()]);await page.close();
    }
    fs.writeFileSync(path.join(out,'rich-browser-checks.json'),JSON.stringify(checks,null,2));
  } finally {await browser.close();}
});

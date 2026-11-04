(() => {
  'use strict';
  const config = document.getElementById('enrichment-config');
  if (!config) return;
  const active = new Set();
  const completedDetails = new Set();
  let timer = null;
  let stopped = false;
  const message = text => { document.getElementById('enrichment-message').textContent = text; };
  const checkAccess = response => {
    if (response.status === 401 || response.status === 403) {
      stopped = true;
      active.clear();
      message(response.status === 401 ? '登录已失效，请重新登录' : '无访问权限');
      throw Error('access');
    }
    if (!response.ok) throw Error('request');
    return response;
  };
  const getPage = async () => {
    const response = checkAccess(await fetch(window.location.href, {credentials: 'same-origin', cache: 'no-store'}));
    if (response.redirected) { stopped = true; message('登录已失效，请重新登录'); throw Error('access'); }
    return new DOMParser().parseFromString(await response.text(), 'text/html');
  };
  async function refreshList() {
    const page = await getPage();
    for (const selector of ['.recording-list, .recording-grid', '.pagination', '.home-heading h1']) {
      const old = document.querySelector(selector);
      const fresh = page.querySelector(selector);
      if (old && fresh) old.replaceWith(fresh);
    }
    const freshConfig = page.getElementById('enrichment-config');
    if (freshConfig) {
      config.dataset.csrf = freshConfig.dataset.csrf;
      watch(JSON.parse(freshConfig.querySelector('script').textContent));
    }
  }
  function retryButton(recordingId) {
    const button = document.createElement('button');
    button.className = 'text-button';
    button.type = 'button';
    button.dataset.enrichmentRetry = recordingId;
    button.textContent = '分析失败 · 重试';
    return button;
  }
  function updateCard(card, record) {
    const tags = card.querySelector('[data-enrichment-tags]');
    if (tags && record.tag) {
      tags.replaceChildren();
      tags.title = (record.tags || []).join(' · ');
      const labels = record.tag === 'done' ? record.tags.slice(0, 3) : [
        record.tag === 'failed' ? (record.tag_error || '标签生成失败') : '标签生成中…'];
      if (record.tag === 'done' && record.tags.length > 3) labels.push('+' + (record.tags.length - 3));
      for (const [index, text] of labels.entries()) {
        const chip = document.createElement('span');
        chip.className = 'tag-pill' + (['pending', 'running'].includes(record.tag) ? ' enrichment-pending' : '');
        if (record.tag === 'done' && index >= 2) chip.className += ' desktop-only';
        chip.textContent = text;
        if (text.startsWith('+')) chip.title = tags.title;
        tags.append(chip);
      }
      if (record.tag === 'done' && record.tags.length > 2) {
        const chip = document.createElement('span');
        chip.className = 'tag-pill mobile-only';
        chip.textContent = '+' + (record.tags.length - 2); chip.title = tags.title;
        tags.append(chip);
      }
    }
    const hotword = card.querySelector('[data-enrichment-hotword]');
    if (hotword && record.hotword) hotword.textContent = ['pending', 'running'].includes(record.hotword) ? '热词分析中…' :
      record.hotword === 'failed' ? (record.hotword_error || '热词分析失败') :
        record.hotword_count ? `热词已应用 ${record.hotword_count} 处` : '';
    const retries = card.querySelector('[data-enrichment-retries]');
    if (retries) {
      retries.replaceChildren();
      if (config.dataset.csrf && (record.tag_retryable || record.hotword_retryable)) retries.append(retryButton(record.recording_id));
    }
  }
  async function refreshDetail(record) {
    if (completedDetails.has(record.hotword) || !['done', 'failed'].includes(record.hotword)) return;
    // Preserve active drafts. The next poll (or edit-end event) applies completion.
    if (document.querySelector('[contenteditable="true"]')) return;
    const page = await getPage();
    const oldTrace = document.getElementById('enrichment-hotword');
    oldTrace.replaceChildren(...page.getElementById('enrichment-hotword').childNodes);
    if (record.hotword === 'done') {
      for (const id of ['summary-content', 'transcript-segments']) {
        const old = document.getElementById(id);
        const fresh = page.getElementById(id);
        if (old && fresh) old.replaceChildren(...fresh.childNodes);
      }
      const source = page.getElementById('edit-source');
      const blocks = page.getElementById('edit-blocks');
      if (source && blocks) document.dispatchEvent(new CustomEvent('enrichment:content', {
        detail: {source: JSON.parse(source.textContent), blocks: JSON.parse(blocks.textContent)}
      }));
      const search = document.getElementById('transcript-search');
      if (search) search.dispatchEvent(new Event('input'));
    }
    completedDetails.add(record.hotword);
  }
  let deferredDetail = null;
  async function apply(result) {
    for (const record of result.records) {
      for (const card of document.querySelectorAll('[data-recording-id]')) {
        if (card.dataset.recordingId === record.recording_id) updateCard(card, record);
      }
      if (config.dataset.recording === record.recording_id) {
        if (document.querySelector('[contenteditable="true"]')) deferredDetail = record;
        else await refreshDetail(record);
      }
    }
  }
  document.addEventListener('enrichment:edit-end', async () => {
    if (deferredDetail) { const record = deferredDetail; deferredDetail = null; await refreshDetail(record); }
  });
  async function poll() {
    timer = null;
    for (const run of [...active]) {
      if (stopped) break;
      try {
        const response = await fetch(`/api/enrichment/status?run=${encodeURIComponent(run)}`, {credentials: 'same-origin', cache: 'no-store'});
        if (response.status === 404) { active.delete(run); continue; }
        checkAccess(response);
        const result = await response.json();
        message('');
        await apply(result);
        if (['done', 'partial', 'failed'].includes(result.status)) active.delete(run);
      } catch (_) { if (!stopped) message('分析状态暂时不可用，正在重试…'); }
    }
    schedule();
  }
  function schedule() { if (!stopped && active.size && timer === null) timer = setTimeout(poll, 2000); }
  function watch(runs) { for (const run of runs) active.add(run); schedule(); }
  document.addEventListener('click', async event => {
    if (event.target.closest('.enrichment-pending')) { event.preventDefault(); return; }
    const button = event.target.closest('[data-enrichment-retry]');
    if (!button || button.disabled) return;
    event.preventDefault();
    button.disabled = true;
    try {
      const response = checkAccess(await fetch('/api/enrichment/retry', {method: 'POST', credentials: 'same-origin',
        body: new URLSearchParams({csrf_token: config.dataset.csrf, recording_id: button.dataset.enrichmentRetry})}));
      const result = await response.json();
      watch(result.runs);
      completedDetails.clear();
      if (config.dataset.recording) {
        const page = await getPage();
        watch(JSON.parse(page.getElementById('enrichment-config').querySelector('script').textContent));
        document.getElementById('enrichment-hotword').replaceChildren(...page.getElementById('enrichment-hotword').childNodes);
      } else await refreshList();
    } catch (_) { if (!stopped) message('重试失败，请刷新页面后重试'); }
    finally { button.disabled = false; }
  });
  window.enrichment = {watch, refreshList, updateCard};
  watch(JSON.parse(config.querySelector('script').textContent));
})();

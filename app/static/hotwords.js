/* Explicit per-word publishing with immediately visible persisted results. */
(() => {
  function error(value) {
    if (!value.trim() || Array.from(value).length > 20) return '请输入 1–20 个字符的词语。';
    if (/[，。！？；：、\n\r\t]/.test(value) || / {2,}/.test(value)) return '词语不能含句子标点、换行或连续两个空格。';
    if (/[<>]/.test(value)) return '词语不能含 < 或 >。';
    return '';
  }
  document.querySelectorAll('.hotword-form').forEach((form, index) => {
    const term = form.elements.term;
    function validate() {
      let hint = form.querySelector('[data-term-error]') || term.parentElement.querySelector('.hotword-field-error');
      const message = error(term.value);
      if (!hint) {
        hint = document.createElement('span'); hint.className = 'hotword-field-error';
        hint.dataset.termError = ''; hint.id = `term-live-error-${index}`;
        hint.setAttribute('role', 'alert'); term.after(hint);
      }
      hint.textContent = message; hint.hidden = !message;
      term.setAttribute('aria-invalid', String(Boolean(message)));
      term.setAttribute('aria-describedby', hint.id);
      return !message;
    }
    term.addEventListener('input', validate);
    form.addEventListener('submit', event => {
      if (!validate()) { event.preventDefault(); term.focus(); }
    });
  });

  const upstream = document.querySelector('.hotword-upstream');
  if (!upstream) return;
  const usage = document.getElementById('qwen-usage');
  const sync = document.getElementById('qwen-sync');
  const pull = document.getElementById('qwen-import');
  const feedback = document.getElementById('qwen-feedback');
  const dialog = document.getElementById('qwen-dialog');
  const title = document.getElementById('qwen-dialog-title');
  const content = document.getElementById('qwen-plan');
  const message = document.getElementById('qwen-dialog-feedback');
  const confirm = document.getElementById('qwen-confirm');
  const close = document.getElementById('qwen-close');
  let busy = false;
  async function post(path, values = {}) {
    const response = await fetch(path, {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({csrf_token:upstream.dataset.csrf, ...values})});
    let result;
    try {result = await response.json();} catch (_) {throw new Error('请求失败，请刷新后重试。');}
    if (!response.ok) throw new Error(result.message || result.error || '请求失败，请刷新后重试。');
    return result;
  }
  function setUsage(value) {
    usage.textContent = `千问热词：已用 ${value.currentCount} / ${value.maxCount}`;
  }
  function element(tag, text) {
    const node = document.createElement(tag); node.textContent = text; return node;
  }
  function showResult(result) {
    if (Number.isInteger(result.currentCount)) setUsage(result);
    const row = document.querySelector(`[data-hotword-id="${result.hotword_id}"]`);
    if (!row) return;
    const pushed = result.status === 'ok' || result.status === 'skipped';
    row.querySelector('[data-qwen-state]').textContent = pushed ? '已推送' : `失败（${result.message}）`;
    const button = row.querySelector('[data-push]');
    button.disabled = pushed; button.textContent = pushed ? '已推送' : '推送到千问';
    row.querySelector('[data-repush]').hidden = !pushed;
  }
  function disablePushes(value) {
    sync.disabled = value;
    document.querySelectorAll('[data-repush]').forEach(b => {b.disabled = value;});
    document.querySelectorAll('[data-push]').forEach(b => {b.disabled = value || b.textContent === '已推送';});
  }
  post('/hotwords/qwennote-status').then(setUsage).catch(() => {
    feedback.textContent = '千问配额读取失败，可重试推送或导入。';
  });
  document.querySelectorAll('[data-hotword-id]').forEach(row => {
    for (const selector of ['[data-push]', '[data-repush]']) {
      row.querySelector(selector).addEventListener('click', async () => {
        if (busy) return;
        busy = true; disablePushes(true); feedback.textContent = '正在推送…';
        try {
          const result = await post(`/hotwords/${row.dataset.hotwordId}/push-qwennote`,
            {force:selector === '[data-repush]' ? '1' : '0'});
          showResult(result); feedback.textContent = result.message;
        } catch (error) {feedback.textContent = error.message;}
        finally {busy = false; disablePushes(false);}
      });
    }
  });
  sync.addEventListener('click', async () => {
    if (busy) return;
    busy = true; disablePushes(true); feedback.textContent = '正在逐条推送…';
    try {
      const result = await post('/hotwords/sync-qwennote');
      result.results.forEach(showResult); feedback.textContent = result.message;
    } catch (error) {feedback.textContent = error.message;}
    finally {busy = false; disablePushes(false);}
  });
  pull.addEventListener('click', async () => {
    pull.disabled = true; feedback.textContent = '';
    try {
      const result = await post('/hotwords/import-preview'); setUsage(result);
      title.textContent = `导入候选 ${result.candidates.length} 条`;
      content.replaceChildren(); message.textContent = ''; close.textContent = '取消';
      content.append(element('p', '勾选要加入本地热词表的标准词。'));
      result.candidates.forEach(term => {
        const label = document.createElement('label'); const input = document.createElement('input');
        input.type = 'checkbox'; input.value = term;
        input.addEventListener('change', () => {confirm.disabled = !content.querySelector('input:checked');});
        label.append(input, element('span', term)); content.append(label);
      });
      confirm.textContent = '添加选中的词'; confirm.disabled = true; dialog.showModal();
    } catch (error) {feedback.textContent = error.message;} finally {pull.disabled = false;}
  });
  close.addEventListener('click', () => {if (!busy) dialog.close();});
  dialog.addEventListener('cancel', event => {if (busy) event.preventDefault();});
  confirm.addEventListener('click', async () => {
    if (busy) return;
    busy = true; confirm.disabled = close.disabled = true; message.textContent = '';
    try {
      let added = 0, existing = 0;
      for (const input of content.querySelectorAll('input:checked')) {
        const result = await post('/hotwords', {term:input.value, from_upstream:'1'});
        added += Number(result.added); existing += Number(result.existing);
        input.checked = false; input.disabled = true;
      }
      message.textContent = `已添加 ${added} 条，已存在 ${existing} 条。`;
      close.textContent = '完成'; close.onclick = () => location.reload();
    } catch (error) {message.textContent = error.message;} finally {
      busy = false; close.disabled = false;
      confirm.disabled = !content.querySelector('input:checked');
    }
  });
  dialog.addEventListener('close', () => {close.onclick = null;});
})();

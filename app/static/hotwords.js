/* Inline feedback; the server validates again and reports skipped aliases. */
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
  const errors = {quota_unknown:'配额尚未读取，请关闭后重试。', quota_insufficient:'剩余配额不足，整批未推送。',
    plan_changed:'词表或配额已变化，请关闭后重新预演。', upstream_failed:'推送结果未确认，请先从千问导入核对，勿盲目重试。'};
  let mode, preview;
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
  function showPlan(result) {
    content.replaceChildren();
    content.append(element('h3', `将推送 ${result.to_push.length} 条`));
    const terms = document.createElement('ul');
    result.to_push.forEach(row => terms.append(element('li', row.term)));
    content.append(terms, element('h3', `跳过 ${result.skipped.length} 条`));
    const skipped = document.createElement('ul');
    result.skipped.forEach(row => skipped.append(element('li', `${row.term}：${row.message}（${row.reason}）`)));
    content.append(skipped, element('p', `推送后占用 ${result.afterCount ?? '未知'} / ${result.maxCount}`),
      element('p', '千问仅支持新增；误推后需到千问 App 手动清理。'));
    if (result.quota_checked_at) content.append(element('p', '配额读取于 ' + new Date(result.quota_checked_at).toLocaleString()));
    message.textContent = errors[result.error] || '';
    confirm.disabled = !result.can_confirm;
  }
  // The status read is separate from planning: confirm=0 sends no MCP requests.
  let statusRead = post('/hotwords/qwennote-status').then(setUsage).catch(() => {
    feedback.textContent = '千问配额读取失败，可重试同步或导入。';
  });
  sync.addEventListener('click', async () => {
    sync.disabled = true; feedback.textContent = '';
    try {
      await statusRead;
      setUsage(await post('/hotwords/qwennote-status'));
      preview = await post('/hotwords/sync-qwennote', {confirm:'0'});
      mode = 'sync'; title.textContent = '同步预演'; confirm.textContent = '确认推送'; close.textContent = '取消';
      showPlan(preview); dialog.showModal();
    } catch (error) {feedback.textContent = error.message;} finally {sync.disabled = false;}
  });
  pull.addEventListener('click', async () => {
    pull.disabled = true; feedback.textContent = '';
    try {
      const result = await post('/hotwords/import-preview'); setUsage(result);
      mode = 'import'; title.textContent = `导入候选 ${result.candidates.length} 条`;
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
      if (mode === 'sync') {
        const result = await post('/hotwords/sync-qwennote', {confirm:'1', plan_hash:preview.plan_hash});
        if (result.error) {
          message.textContent = errors[result.error] || '推送失败。';
          // Do not offer an automatic write retry after an uncertain result.
        } else {
          setUsage({currentCount:result.afterCount, maxCount:result.maxCount});
          message.textContent = `已推送 ${result.to_push.length} 条。`; close.textContent = '完成';
          close.onclick = () => location.reload();
        }
      } else {
        let added = 0, existing = 0;
        for (const input of content.querySelectorAll('input:checked')) {
          const result = await post('/hotwords', {term:input.value, from_upstream:'1'});
          added += Number(result.added); existing += Number(result.existing);
          input.checked = false; input.disabled = true;
        }
        message.textContent = `已添加 ${added} 条，已存在 ${existing} 条。`;
        close.textContent = '完成'; close.onclick = () => location.reload();
      }
    } catch (error) {message.textContent = error.message;} finally {
      busy = false; close.disabled = false;
      if (mode === 'import') confirm.disabled = !content.querySelector('input:checked');
    }
  });
  dialog.addEventListener('close', () => {close.onclick = null;});
})();

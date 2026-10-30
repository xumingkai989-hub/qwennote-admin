'use strict';
(() => {
  const form = document.getElementById('recording-editor');
  if (!form) return;
  const edit = document.getElementById('edit-button');
  const content = document.getElementById('edit-content');
  const display = document.getElementById('detail-display');
  const status = document.getElementById('edit-status');
  const save = document.getElementById('save-edit');
  const cancel = document.getElementById('cancel-edit');
  const dialog = document.getElementById('hotword-suggestions');
  const options = document.getElementById('hotword-options');
  const saveWords = document.getElementById('save-hotwords');
  const dismiss = document.getElementById('dismiss-hotwords');
  const wordStatus = document.getElementById('hotword-status');
  const baseline = content.value;
  const fields = () => new URLSearchParams(new FormData(form));
  let suggestions = [];
  let busy = false;
  let wordsBusy = false;

  async function post(url, body) {
    const response = await fetch(url, {method: 'POST', credentials: 'same-origin', body});
    if (!response.ok) throw Error('request failed');
    return response.json();
  }
  edit.addEventListener('click', () => {
    form.hidden = false;
    display.hidden = true;
    edit.hidden = true;
    status.textContent = '';
    content.focus();
  });
  cancel.addEventListener('click', () => {
    if (busy) return;
    content.value = baseline;
    form.hidden = true;
    display.hidden = false;
    edit.hidden = false;
    status.textContent = '';
    edit.focus();
  });
  function selected() {
    return Array.from(options.querySelectorAll('input:checked')).map(input => suggestions[Number(input.value)]);
  }
  function selectionChanged() {
    const count = selected().length;
    saveWords.textContent = `存入选中的 ${count} 条`;
    saveWords.disabled = !count || wordsBusy;
  }
  options.addEventListener('change', selectionChanged);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    save.disabled = cancel.disabled = true;
    status.textContent = '正在保存…';
    try {
      const result = await post(form.action, fields());
      // Saving is complete even when the optional hotword action is dismissed or fails.
      suggestions = result.suggestions;
      if (!suggestions.length) { window.location.reload(); return; }
      form.hidden = true;
      const preview = document.getElementById('saved-preview');
      preview.textContent = content.value;
      preview.hidden = false;
      document.getElementById('edit-trace-label').textContent = `已手动编辑 · ${result.edited_at}`;
      document.getElementById('edit-trace').hidden = false;
      status.textContent = '已保存';
      document.getElementById('hotword-suggestion-title').textContent = `检测到 ${suggestions.length} 处修改，是否存入热词？`;
      options.replaceChildren();
      suggestions.forEach((item, index) => {
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = String(index);
        checkbox.checked = true;
        const text = document.createElement('span');
        text.textContent = `${item.source} → ${item.target}`;
        label.append(checkbox, text);
        options.append(label);
      });
      selectionChanged();
      dialog.showModal();
    } catch (error) {
      status.textContent = '保存失败，请重试；若登录已过期，请刷新后重新登录。';
    } finally {
      busy = false;
      save.disabled = cancel.disabled = false;
    }
  });
  // Escape and “不用了” only dismiss. Neither path sends a hotword request.
  dismiss.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => window.location.reload());
  dialog.addEventListener('cancel', event => { if (wordsBusy) event.preventDefault(); });
  saveWords.addEventListener('click', async () => {
    if (wordsBusy || !selected().length) return;
    wordsBusy = true;
    saveWords.disabled = dismiss.disabled = true;
    options.querySelectorAll('input').forEach(input => { input.disabled = true; });
    try {
      const body = fields();
      body.delete('content');
      body.delete('field');
      body.set('items', JSON.stringify(selected()));
      const result = await post('/hotwords/bulk', body);
      wordStatus.textContent = result.message;
      dismiss.textContent = '完成';
      saveWords.hidden = true;
    } catch (error) {
      wordStatus.textContent = '热词未存入，请重试。正文已保存。';
    } finally {
      wordsBusy = false;
      dismiss.disabled = false;
      options.querySelectorAll('input').forEach(input => { input.disabled = false; });
      selectionChanged();
    }
  });
  document.getElementById('revert-button').addEventListener('click', async event => {
    if (busy) return;
    busy = true;
    event.target.disabled = true;
    try {
      const body = fields();
      body.delete('content');
      await post(form.action.replace(/\/edit$/, '/revert'), body);
      window.location.reload();
    } catch (error) {
      status.textContent = '还原失败，请刷新后重试。';
      event.target.disabled = false;
    } finally { busy = false; }
  });
})();

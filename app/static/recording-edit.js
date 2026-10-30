'use strict';
(() => {
  const form = document.getElementById('recording-editor');
  if (!form) return;
  const edit = document.getElementById('edit-button');
  const content = document.getElementById('summary-content') || document.getElementById('transcript-segments');
  const card = content.closest('.detail-section');
  const display = document.getElementById('detail-display');
  const status = document.getElementById('edit-status');
  const save = edit;
  const cancel = document.getElementById('cancel-edit');
  const dialog = document.getElementById('hotword-suggestions');
  const options = document.getElementById('hotword-options');
  const saveWords = document.getElementById('save-hotwords');
  const dismiss = document.getElementById('dismiss-hotwords');
  const wordStatus = document.getElementById('hotword-status');
  const baseline = JSON.parse(document.getElementById('edit-source').textContent);
  const blocks = JSON.parse(document.getElementById('edit-blocks').textContent);
  const originalHTML = content.innerHTML;
  const originalBlocks = new WeakMap();
  Array.from(content.children).forEach((node, index) => {
    originalBlocks.set(node, {html: node.outerHTML, source: blocks[index]});
  });
  let editing = false;
  let dirty = false;
  content.addEventListener('input', () => { dirty = true; });

  // Convert only changed blocks. Untouched blocks retain their original Markdown.
  function markdown(node, plain = false, depth = 0) {
    if (node.nodeType === Node.TEXT_NODE) return plain ? node.textContent : node.textContent.replace(/([\\`*_{}\[\]<>])/g, '\\$1');
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const tag = node.tagName;
    const children = () => Array.from(node.childNodes).map(child => markdown(child, plain, depth)).join('');
    if (tag === 'BR') return '\n';
    if (plain) return children() + (/^(P|DIV|H[1-6]|LI)$/.test(tag) ? '\n\n' : '');
    if (/^H[1-6]$/.test(tag)) return '#'.repeat(Number(tag[1])) + ' ' + children().trim() + '\n\n';
    if (tag === 'STRONG' || tag === 'B') return '**' + children() + '**';
    if (tag === 'EM' || tag === 'I') return '*' + children() + '*';
    if (tag === 'S' || tag === 'DEL') return '~~' + children() + '~~';
    if (tag === 'A') return '[' + children() + '](' + (node.getAttribute('href') || '').replace(/\)/g, '%29') + ')';
    if (tag === 'IMG') return '![' + (node.getAttribute('alt') || '') + '](' + (node.getAttribute('src') || '') + ')';
    if (tag === 'INPUT') return node.checked ? '[x] ' : '[ ] ';
    if (tag === 'PRE') {
      const code = node.querySelector('code');
      const text = node.textContent;
      const fence = '`'.repeat(Math.max(3, ...Array.from(text.matchAll(/`+/g), item => item[0].length + 1)));
      return fence + (code?.className.match(/language-(\S+)/)?.[1] || '') + '\n' + text.replace(/\n$/, '') + '\n' + fence + '\n\n';
    }
    if (tag === 'CODE') {
      const fence = '`'.repeat(Math.max(1, ...Array.from(node.textContent.matchAll(/`+/g), item => item[0].length + 1)));
      return fence + ' ' + node.textContent + ' ' + fence;
    }
    if (tag === 'HR') return '---\n\n';
    if (tag === 'BLOCKQUOTE') return children().trim().split('\n').map(line => '> ' + line).join('\n') + '\n\n';
    if (tag === 'UL' || tag === 'OL') {
      return Array.from(node.children).map((li, index) => {
        const prefix = tag === 'OL' ? String(Number(node.getAttribute('start') || 1) + index) + '. ' : '- ';
        const body = Array.from(li.childNodes).map(child => markdown(child, false, depth + 1)).join('').trim();
        return prefix + body.replace(/\n/g, '\n' + ' '.repeat(prefix.length));
      }).join('\n') + '\n\n';
    }
    if (tag === 'TABLE') {
      const rows = Array.from(node.querySelectorAll('tr')).map(tr => '| ' + Array.from(tr.children).map(cell => markdown(cell).trim().replace(/\n/g, '<br>').replace(/\|/g, '\\|')).join(' | ') + ' |');
      if (rows.length) rows.splice(1, 0, '| ' + Array.from(node.querySelector('tr').children, () => '---').join(' | ') + ' |');
      return rows.join('\n') + '\n\n';
    }
    return children() + (/^(P|DIV)$/.test(tag) ? '\n\n' : '');
  }
  function editedMarkdown() {
    if (!dirty || content.innerHTML === originalHTML) return baseline;
    if (content.id === 'transcript-segments') {
      return Array.from(content.childNodes).map(node => {
        if (node.nodeType === Node.ELEMENT_NODE && node.classList.contains('transcript-segment')) {
          const text = markdown(node.querySelector('.transcript-body'), true).trim();
          return [node.dataset.sourceHeader, text].filter(Boolean).join('\n\n');
        }
        return markdown(node, true).trim();
      }).filter(Boolean).join('\n\n');
    }
    return Array.from(content.childNodes).map(node => {
      const original = originalBlocks.get(node);
      return original && original.html === node.outerHTML && original.source !== undefined
        ? original.source : markdown(node).trim();
    }).filter(text => text.trim()).join('\n\n');
  }
  function editState(active) {
    editing = active;
    content.contentEditable = String(active);
    content.setAttribute('role', active ? 'textbox' : 'document');
    content.setAttribute('aria-multiline', String(active));
    card.classList.toggle('is-editing', active);
    edit.setAttribute('aria-label', active ? '完成' : '编辑');
    edit.setAttribute('title', active ? '完成' : '编辑');
    edit.setAttribute('aria-pressed', String(active));
    edit.querySelector('.pencil-icon').toggleAttribute('hidden', active);
    edit.querySelector('.check-icon').toggleAttribute('hidden', !active);
    cancel.hidden = !active;
    card.querySelectorAll('.empty-state').forEach(node => { node.hidden = active; });
    ['transcript-search', 'copy-transcript', 'revert-button'].forEach(id => {
      const node = document.getElementById(id);
      if (node) node.disabled = active;
    });
    content.querySelectorAll('.speaker-avatar, .speaker-name, .timestamp').forEach(node => {
      if (active) node.setAttribute('contenteditable', 'false');
      else node.removeAttribute('contenteditable');
    });
    content.querySelectorAll('.transcript-segment').forEach(node => {
      node.setAttribute('role', active ? 'group' : 'button');
      node.setAttribute('tabindex', active ? '-1' : '0');
    });
  }
  content.addEventListener('paste', event => {
    if (!editing) return;
    event.preventDefault();
    const selection = window.getSelection();
    if (!selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    range.deleteContents();
    const fragment = document.createDocumentFragment();
    event.clipboardData.getData('text/plain').split(/\r?\n/).forEach((line, index) => {
      if (index) fragment.append(document.createElement('br'));
      fragment.append(document.createTextNode(line));
    });
    const last = fragment.lastChild;
    range.insertNode(fragment);
    if (last) range.setStartAfter(last);
    dirty = true;
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
  });
  document.addEventListener('keydown', event => {
    if (!editing || event.isComposing) return;
    if (event.key === 'Escape') { event.preventDefault(); cancel.click(); }
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); form.requestSubmit(); }
  });
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
    if (busy) return;
    if (editing) { form.requestSubmit(); return; }
    const search = document.getElementById('transcript-search');
    if (search) { search.value = ''; search.dispatchEvent(new Event('input')); }
    dirty = false;
    editState(true);
    status.textContent = '';
    content.focus({preventScroll:true});
  });
  cancel.addEventListener('click', () => {
    if (busy) return;
    content.innerHTML = originalHTML;
    Array.from(content.children).forEach((node, index) => { originalBlocks.set(node, {html: node.outerHTML, source: blocks[index]}); });
    editState(false);
    status.textContent = '';
    edit.focus({preventScroll:true});
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
    if (busy || !editing) return;
    const body = fields();
    const draft = editedMarkdown();
    if (draft.length > 200000) { status.textContent = '正文最多 200000 字。'; return; }
    body.set('content', draft);
    busy = true;
    save.disabled = cancel.disabled = true;
    status.textContent = '正在保存…';
    try {
      const result = await post(form.action, body);
      // Saving is complete even when the optional hotword action is dismissed or fails.
      suggestions = result.suggestions;
      if (!suggestions.length) { window.location.reload(); return; }
      editState(false);
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
        text.textContent = item.term;
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

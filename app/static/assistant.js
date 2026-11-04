(() => {
  const input = document.getElementById('instructions');
  const save = document.getElementById('instructions-save');
  const error = document.getElementById('instructions-error');
  function validate() {
    const length = Array.from(input.value).length;
    save.disabled = length > 2000;
    input.setAttribute('aria-invalid', String(save.disabled));
    document.getElementById('instructions-count').textContent = `${length} / 2000`;
    error.textContent = save.disabled ? '反馈指令最多 2000 字，请缩短后保存。' : '';
  }
  input.addEventListener('input', () => {
    document.getElementById('instructions-saved').textContent = '';
    validate();
  });
  document.getElementById('instructions-form').addEventListener('submit', event => {
    validate();
    if (save.disabled) { event.preventDefault(); input.focus(); }
  });
  document.querySelectorAll('.merge-form').forEach(form => {
    form.addEventListener('submit', event => {
      const target = form.querySelector('select').selectedOptions[0];
      if (!target?.value || !window.confirm(`将「${form.dataset.source}」合并到「${target.textContent}」？已有记录的标签也会同步改写。`)) event.preventDefault();
    });
  });
  document.querySelectorAll('.category-title').forEach(title => {
    const button = title.querySelector('.category-edit-btn');
    const name = title.querySelector('.category-name');
    const form = title.querySelector('.category-rename');
    const input = form.querySelector('input[name="name"]');
    const size = form.querySelector('.category-input-size');
    const error = document.getElementById('assistant-error');
    let editing = false;
    const resize = () => { size.textContent = input.value || ' '; };
    function close() {
      editing = false;
      form.hidden = true;
      name.hidden = false;
    }
    button.addEventListener('click', () => {
      if (editing || button.disabled) return;
      editing = true;
      input.value = name.textContent;
      resize();
      name.hidden = true;
      form.hidden = false;
      input.focus();
      input.select();
    });
    input.addEventListener('input', resize);
    async function save(refocus = false) {
      if (!editing) return;
      const original = name.textContent;
      const next = input.value.trim();
      // Close before disabling or focusing controls so blur cannot submit twice.
      close();
      if (next === original) {
        if (refocus) button.focus();
        return;
      }
      error.textContent = '';
      if (!next || Array.from(next).length > 40) {
        input.value = original;
        error.textContent = '分类名称须为 1–40 字，请重新编辑。';
        if (refocus) button.focus();
        return;
      }
      input.value = next;
      const body = new URLSearchParams(new FormData(form));
      button.disabled = true;
      title.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(form.action, {
          method: 'POST', body, credentials: 'same-origin'
        });
        // The existing route redirects to the refreshed page after saving.
        // Verify that page, rather than treating a login redirect as success.
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const savedTitle = page.getElementById(title.id);
        const savedName = savedTitle?.querySelector('.category-name')?.textContent;
        if (!response.ok || savedName !== next) {
          throw new Error(page.getElementById('assistant-error')?.textContent || '保存失败，请刷新后重试');
        }
        name.textContent = savedName;
        button.setAttribute('aria-label', `重命名 ${savedName}`);
        input.setAttribute('aria-label', `分类名称 ${savedName}`);
        const categoryId = body.get('category_id');
        const optionText = page.querySelector(`.tag-category-form option[value="${categoryId}"]`)?.textContent;
        if (optionText) document.querySelectorAll(`.tag-category-form option[value="${categoryId}"]`).forEach(option => {
          option.textContent = optionText;
        });
        const changes = page.querySelector('.change-list');
        if (changes) document.querySelector('.change-list').replaceChildren(...changes.childNodes);
      } catch (failure) {
        name.textContent = original;
        input.value = original;
        error.textContent = `保存失败：${failure.message || '请刷新后重试'}`;
      } finally {
        button.disabled = false;
        title.setAttribute('aria-busy', 'false');
        if (refocus) button.focus();
      }
    }
    form.addEventListener('submit', event => { event.preventDefault(); save(true); });
    input.addEventListener('blur', () => save());
    input.addEventListener('keydown', event => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        input.value = name.textContent;
        close();
        button.focus();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        save(true);
      }
    });
  });
  document.querySelectorAll('.tag-category-form').forEach(form => {
    const select = form.querySelector('select');
    const status = form.querySelector('.category-save-status');
    select.addEventListener('change', async () => {
      const body = new URLSearchParams(new FormData(form));
      const categoryId = select.value;
      select.disabled = true;
      status.textContent = '保存中…';
      status.classList.remove('assistant-error');
      try {
        const response = await fetch(form.action, {
          method: 'POST', body, headers: {'Accept': 'application/json'}, credentials: 'same-origin'
        });
        if (!response.ok) throw new Error('save failed');
        const result = await response.json();
        if (!result.saved) throw new Error('save failed');
        select.dataset.savedValue = categoryId;
        const target = document.getElementById(`category-body-${categoryId}`);
        const row = form.closest('.tag-row');
        const following = [...target.children].find(other =>
          Number(other.dataset.usageCount) < Number(row.dataset.usageCount) ||
          (Number(other.dataset.usageCount) === Number(row.dataset.usageCount) &&
           Number(other.dataset.tagId) > Number(row.dataset.tagId)));
        target.insertBefore(row, following || null);
        document.querySelectorAll('.tag-category').forEach(group => {
          group.querySelector('.category-count').textContent = `（${group.querySelectorAll('.tag-row').length}）`;
        });
        status.textContent = '已保存';
      } catch (_) {
        select.value = select.dataset.savedValue;
        status.textContent = '保存失败，请刷新后重试';
        status.classList.add('assistant-error');
      } finally { select.disabled = false; }
    });
  });
  validate();
})();

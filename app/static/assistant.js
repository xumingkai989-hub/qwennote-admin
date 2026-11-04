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
  document.querySelectorAll('.category-toggle').forEach(button => {
    button.addEventListener('click', () => {
      const body = document.getElementById(button.getAttribute('aria-controls'));
      body.hidden = !body.hidden;
      button.setAttribute('aria-expanded', String(!body.hidden));
      button.textContent = body.hidden ? '▸' : '▾';
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
        target.querySelector('tbody').appendChild(form.closest('tr'));
        const toggle = target.closest('.tag-category').querySelector('.category-toggle');
        target.hidden = false;
        toggle.setAttribute('aria-expanded', 'true');
        toggle.textContent = '▾';
        document.querySelectorAll('.tag-category').forEach(group => {
          group.querySelector('.category-count').textContent = `（${group.querySelectorAll('tbody tr').length}）`;
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

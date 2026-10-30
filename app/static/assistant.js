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
  validate();
})();

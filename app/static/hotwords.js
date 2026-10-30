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
})();

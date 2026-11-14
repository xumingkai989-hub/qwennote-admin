document.querySelectorAll('.knowledge-copy').forEach(button => {
  const originalLabel = button.textContent;
  let resetTimer;
  button.addEventListener('click', async () => {
    const code = document.getElementById(button.dataset.copy);
    const status = document.querySelector('.knowledge-copy-status');
    const label = button.dataset.copyLabel;
    try {
      await navigator.clipboard.writeText(code.textContent);
      clearTimeout(resetTimer);
      button.textContent = '已复制';
      resetTimer = setTimeout(() => { button.textContent = originalLabel; }, 2000);
      status.textContent = label + '已复制';
    } catch (_) {
      clearTimeout(resetTimer);
      button.textContent = originalLabel;
      code.focus();
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = '已选中' + label + '，请手动复制';
    }
  });
});

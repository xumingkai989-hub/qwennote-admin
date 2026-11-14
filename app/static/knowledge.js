document.querySelectorAll('.knowledge-copy').forEach(button => {
  button.addEventListener('click', async () => {
    const code = document.getElementById(button.dataset.copy);
    const status = document.querySelector('.knowledge-copy-status');
    try {
      await navigator.clipboard.writeText(code.textContent);
      status.textContent = '命令已复制';
    } catch (_) {
      code.focus();
      const range = document.createRange();
      range.selectNodeContents(code);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = '已选中命令，请手动复制';
    }
  });
});

'use strict';
const copyButton = document.getElementById('copy-markdown');
copyButton.addEventListener('click', async () => {
  const source = document.getElementById('markdown-source');
  const status = document.getElementById('copy-status');
  try {
    await navigator.clipboard.writeText(source.value);
    status.textContent = 'Markdown 已复制';
  } catch (error) {
    document.getElementById('copy-fallback').hidden = false;
    source.focus();
    source.select();
    source.setSelectionRange(0, source.value.length);
    status.textContent = '自动复制失败，已全选 Markdown，请按 Ctrl+C 或 ⌘C 复制。';
  }
});

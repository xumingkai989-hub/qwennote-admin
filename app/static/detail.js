'use strict';
const copyButton = document.getElementById('copy-markdown');
copyButton.addEventListener('click', async () => {
  const source = document.getElementById('markdown-source');
  const status = document.getElementById('copy-status');
  copyButton.disabled = true;
  status.textContent = '正在获取 Markdown…';
  try {
    const response = await fetch(copyButton.dataset.markdownUrl, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok || !response.headers.get('content-type')?.startsWith('text/markdown')) {
      throw new Error('Markdown unavailable');
    }
    source.value = await response.text();
    try {
      await navigator.clipboard.writeText(source.value);
      document.getElementById('copy-fallback').hidden = true;
      status.textContent = 'Markdown 已复制';
    } catch (error) {
      document.getElementById('copy-fallback').hidden = false;
      source.focus();
      source.select();
      source.setSelectionRange(0, source.value.length);
      status.textContent = '自动复制失败，已全选 Markdown，请按 Ctrl+C 或 ⌘C 复制。';
    }
  } catch (error) {
    status.textContent = '获取 Markdown 失败，请重试或使用下载 .md。';
  } finally {
    copyButton.disabled = false;
  }
});

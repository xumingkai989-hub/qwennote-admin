'use strict';
const copyButton = document.getElementById('copy-markdown');
const exportButton = document.getElementById('export-button');
const exportMenu = document.getElementById('export-menu');
const exportContainer = document.getElementById('detail-export');
const exportItems = Array.from(exportMenu.querySelectorAll('[role="menuitem"]'));
function closeExport(restoreFocus = false) {
  exportMenu.hidden = true;
  exportButton.setAttribute('aria-expanded', 'false');
  if (restoreFocus) exportButton.focus();
}
function openExport(index = 0) {
  exportMenu.hidden = false;
  exportButton.setAttribute('aria-expanded', 'true');
  exportItems[index].focus();
}
exportButton.addEventListener('click', () => {
  if (exportMenu.hidden) openExport();
  else closeExport(true);
});
exportButton.addEventListener('keydown', event => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    openExport(event.key === 'ArrowUp' ? exportItems.length - 1 : 0);
  }
});
exportMenu.addEventListener('keydown', event => {
  const index = exportItems.indexOf(document.activeElement);
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    const step = event.key === 'ArrowDown' ? 1 : -1;
    exportItems[(index + step + exportItems.length) % exportItems.length].focus();
  } else if (event.key === 'Home' || event.key === 'End') {
    event.preventDefault();
    exportItems[event.key === 'Home' ? 0 : exportItems.length - 1].focus();
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    if (!event.repeat && index !== -1) exportItems[index].click();
  }
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !exportMenu.hidden) {
    event.preventDefault();
    closeExport(true);
  }
});
for (const eventName of ['click', 'focusin']) {
  document.addEventListener(eventName, event => {
    if (!exportContainer.contains(event.target)) closeExport();
  });
}
document.getElementById('download-markdown').addEventListener('click', () => closeExport(true));
let copyPending = false;
let copyTimer;
copyButton.addEventListener('click', async () => {
  closeExport(true);
  if (copyPending) return;
  const source = document.getElementById('markdown-source');
  const status = document.getElementById('copy-status');
  copyPending = true;
  clearTimeout(copyTimer);
  copyButton.setAttribute('aria-disabled', 'true');
  copyButton.textContent = '复制 Markdown';
  status.textContent = '';
  try {
    const response = await fetch(copyButton.dataset.markdownUrl, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok || !response.headers.get('content-type')?.startsWith('text/markdown')) {
      throw new Error('Markdown unavailable');
    }
    source.value = await response.text();
    try {
      await navigator.clipboard.writeText(source.value);
      document.getElementById('copy-fallback').hidden = true;
      copyButton.textContent = '已复制';
      copyTimer = setTimeout(() => { copyButton.textContent = '复制 Markdown'; }, 800);
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
    copyPending = false;
    copyButton.setAttribute('aria-disabled', 'false');
  }
});

const transcript = document.getElementById('transcript-segments');
if (transcript) {
  const segments = Array.from(transcript.querySelectorAll('.transcript-segment'));
  const toast = document.getElementById('transcript-toast');
  let toastTimer;
  function notify(message) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
  }
  function segmentText(segment) {
    return Array.from(segment.querySelectorAll('.transcript-paragraph'))
      .map(paragraph => paragraph.textContent).join('\n\n');
  }
  function copyText(segment) {
    return [segment.dataset.timestamp, segment.dataset.speaker].filter(Boolean).join(' ') +
      '\n' + segmentText(segment);
  }
  async function copyTranscript(text, message) {
    const fallback = document.getElementById('transcript-copy-fallback');
    try {
      await navigator.clipboard.writeText(text);
      fallback.hidden = true;
      notify(message);
    } catch (error) {
      fallback.hidden = false;
      const source = document.getElementById('transcript-source');
      source.value = text;
      source.focus();
      source.select();
      source.setSelectionRange(0, text.length);
      notify('自动复制失败，已全选文字，请按 Ctrl+C 或 ⌘C 复制。');
    }
  }
  for (const segment of segments) {
    const copy = () => copyTranscript(copyText(segment),
      segment.dataset.timestamp ? `已复制 ${segment.dataset.timestamp}` : '已复制该段');
    segment.addEventListener('click', copy);
    segment.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (!event.repeat) copy();
      }
    });
  }
  document.getElementById('copy-transcript').addEventListener('click', () => {
    copyTranscript(segments.map(copyText).join('\n\n'), '已复制全文');
  });
  const searchStatus = document.getElementById('transcript-search-status');
  const searchable = segments.map(segment =>
    `${segment.dataset.speaker}\n${segment.dataset.timestamp}\n${segmentText(segment)}`.toLocaleLowerCase());
  document.getElementById('transcript-search').addEventListener('input', event => {
    const query = event.target.value.trim().toLocaleLowerCase();
    let firstMatch;
    let count = 0;
    segments.forEach((segment, index) => {
      const matches = !query || searchable[index].includes(query);
      segment.hidden = !matches;
      segment.classList.toggle('is-match', Boolean(query) && matches);
      if (matches) {
        firstMatch ??= segment;
        count += 1;
      }
    });
    searchStatus.hidden = !query;
    searchStatus.textContent = count ? `找到 ${count} 段` : '没有找到匹配的段落';
    if (query && firstMatch) firstMatch.scrollIntoView({ block: 'nearest' });
  });
}

(() => {
  const button = document.getElementById('sync-button');
  const fullButton = document.getElementById('full-sync-button');
  const buttons = [button, fullButton].filter(Boolean);
  const errorMessage = document.getElementById('sync-error');
  const feedback = document.getElementById('sync-feedback');
  const resultText = result => `${result.mode === 'full' ? '全盘同步' : '快速同步'} · 检查 ${result.fetched} 条 · 新增 ${result.inserted} · 更新 ${result.updated} · 跳过 ${result.skipped} · 耗时 ${result.duration_ms} 毫秒${result.detail_serialized ? ' · 限流后已降为串行' : ''}`;
  const sync = async (activeButton, full) => {
    if (activeButton.disabled) return;
    buttons.forEach(item => {item.disabled = true;});
    activeButton.setAttribute('aria-busy', 'true');
    activeButton.classList.add('is-syncing');
    const label = full ? activeButton.querySelector('.sync-label') : null;
    if (label) label.textContent = '正在扫描…';
    errorMessage.textContent = '';
    feedback.textContent = full ? '正在全量扫描，可能需要几分钟' : '正在快速同步';
    window.mobileDensity?.syncPending(full);
    let result;
    try {
      const response = await fetch(full ? '/sync?full=1' : '/sync', {method: 'POST', credentials: 'same-origin'});
      result = await response.json();
      if (!response.ok || result.status !== 'success') {
        throw new Error(result.error || result.detail || '同步请求失败，请重试');
      }
      activeButton.classList.remove('is-syncing');
      activeButton.classList.add('is-complete');
      feedback.textContent = resultText(result);
      window.mobileDensity?.syncResult(result);
      await window.enrichment.refreshList();
      if (result.enrichment_run_id) window.enrichment.watch([result.enrichment_run_id]);
    } catch (error) {
      errorMessage.textContent = error.message || '同步请求失败，请重试';
      window.mobileDensity?.syncResult({...result, status: 'failed'});
      feedback.textContent = result && Number.isInteger(result.duration_ms) ? resultText(result) : '同步失败';
    } finally {
      activeButton.classList.remove('is-syncing', 'is-complete');
      activeButton.removeAttribute('aria-busy');
      if (label) label.textContent = '全盘同步';
      buttons.forEach(item => {item.disabled = false;});
    }
  };
  button.addEventListener('click', () => sync(button, false));
  if (fullButton) fullButton.addEventListener('click', () => sync(fullButton, true));
})();

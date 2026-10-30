(() => {
  const button = document.getElementById('sync-button');
  const errorMessage = document.getElementById('sync-error');
  const feedback = document.getElementById('sync-feedback');
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.classList.add('is-syncing');
    errorMessage.textContent = '';
    feedback.textContent = '正在同步';
    try {
      const response = await fetch('/sync', {method: 'POST', credentials: 'same-origin'});
      const result = await response.json();
      if (!response.ok || result.status !== 'success') {
        throw new Error(result.error || result.detail || '同步请求失败，请重试');
      }
      button.classList.remove('is-syncing');
      button.classList.add('is-complete');
      feedback.textContent = '同步完成';
      await new Promise(resolve => setTimeout(resolve, 1000));
      window.location.reload();
    } catch (error) {
      errorMessage.textContent = error.message || '同步请求失败，请重试';
      feedback.textContent = '同步失败';
    } finally {
      button.classList.remove('is-syncing', 'is-complete');
      button.removeAttribute('aria-busy');
      button.disabled = false;
    }
  });
})();

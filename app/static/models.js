'use strict';
for (const form of document.querySelectorAll('[data-model-test]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const feedback = document.getElementById('model-feedback');
    button.disabled = true;
    button.textContent = '测试中…';
    feedback.textContent = '';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new URLSearchParams(new FormData(form)),
        headers: {'Accept': 'application/json'}, redirect: 'error'
      });
      const result = await response.json();
      feedback.textContent = result.ok ? '连接成功' : result.error || '测试失败，请重试。';
    } catch {
      feedback.textContent = '测试失败，请刷新页面后重试。';
    } finally {
      button.disabled = false;
      button.textContent = '测试';
    }
  });
}
for (const form of document.querySelectorAll('[data-model-delete]')) {
  form.addEventListener('submit', event => {
    if (!window.confirm('确定删除此模型？')) event.preventDefault();
  });
}

// Native forms retain CSRF and redirect behavior while preventing double clicks.
document.querySelectorAll('.board-generate').forEach(form => {
  form.addEventListener('submit', () => {
    document.querySelectorAll('.board-generate button').forEach(button => {
      button.disabled = true;
    });
    const button = form.querySelector('button');
    button.textContent = '生成中…';
    form.setAttribute('aria-busy', 'true');
  });
});

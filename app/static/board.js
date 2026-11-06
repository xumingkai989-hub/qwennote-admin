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

// Match each sector and legend row without using a color as an identifier.
document.querySelectorAll('.board-donut-layout').forEach(chart => {
  const parts = chart.querySelectorAll('[data-chart-key]');
  const highlight = key => parts.forEach(part => {
    part.classList.toggle('is-active', part.dataset.chartKey === key);
  });
  parts.forEach(part => {
    part.addEventListener('pointerenter', () => highlight(part.dataset.chartKey));
    part.addEventListener('pointerleave', () => highlight(null));
    part.addEventListener('focusin', () => highlight(part.dataset.chartKey));
    part.addEventListener('focusout', () => highlight(null));
  });
});

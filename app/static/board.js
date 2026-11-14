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

// Both levels are rendered once; switching stays local and replaces one panel.
document.querySelectorAll('.board-allocation').forEach(section => {
  const buttons = section.querySelectorAll('[data-allocation-view]');
  const panels = section.querySelectorAll('[data-allocation-panel]');
  const select = button => {
    buttons.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    panels.forEach(panel => {
      panel.hidden = panel.dataset.allocationPanel !== button.dataset.allocationView;
    });
  };
  buttons.forEach(button => button.addEventListener('click', () => select(button)));
  const selected = [...buttons].find(button => button.getAttribute('aria-pressed') === 'true') || buttons[0];
  if (selected) select(selected);
});

// Keep SVG text, row height and corner radii in CSS pixels at every width.
// Only the plot's horizontal coordinates scale; all eight weeks remain readable.
document.querySelectorAll('.board-comparison-plot').forEach(plot => {
  const viewport = plot.parentElement;
  const resize = () => {
    const width = viewport.getBoundingClientRect().width - 124;
    if (!width) return;
    plot.setAttribute('viewBox', `0 0 ${width} ${plot.getAttribute('height')}`);
    plot.querySelectorAll('rect').forEach(rect => {
      rect.setAttribute('x', Number(rect.dataset.start) * width);
      rect.setAttribute('width', Number(rect.dataset.width) * width);
    });
    plot.querySelectorAll('polygon').forEach(polygon => {
      const p = polygon.dataset;
      polygon.setAttribute('points', [
        [p.start1 * width, p.y1], [p.end1 * width, p.y1],
        [p.end2 * width, p.y2], [p.start2 * width, p.y2],
      ].map(point => point.join(',')).join(' '));
    });
  };
  resize();
  new ResizeObserver(resize).observe(viewport);
});

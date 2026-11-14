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

// Scale only horizontal geometry. Heights and label font sizes stay in CSS pixels.
document.querySelectorAll('.board-comparison-plot').forEach(plot => {
  const viewport = plot.parentElement;
  const resize = () => {
    const width = plot.getBoundingClientRect().width;
    if (width <= 0) return;
    plot.setAttribute('viewBox', `0 0 ${width} ${plot.getAttribute('height')}`);
    plot.querySelectorAll('rect').forEach(rect => {
      rect.setAttribute('x', Number(rect.dataset.x) * width);
      rect.setAttribute('width', Number(rect.dataset.width) * width);
    });
    plot.querySelectorAll('polygon').forEach(polygon => {
      const p = polygon.dataset;
      polygon.setAttribute('points', [
        [p.x1 * width, p.top1], [p.x2 * width, p.top2],
        [p.x2 * width, p.bottom2], [p.x1 * width, p.bottom1],
      ].map(point => point.join(',')).join(' '));
    });
    const labels = viewport.querySelectorAll('[data-week-label]');
    const stride = Math.max(1, Math.ceil(40 / (width / labels.length)));
    const placedTotals = [];
    viewport.querySelectorAll('[data-total-label]').forEach(label => {
      label.style.transform = 'none';
      label.setAttribute('text-anchor', 'middle');
      const halfText = label.getComputedTextLength() / 2 + 1;
      label.setAttribute('x', Math.max(halfText, Math.min(width - halfText, Number(label.dataset.cx) * width)));
      let y = Number(label.dataset.y);
      label.setAttribute('y', y);
      let box = label.getBoundingClientRect();
      // Nearby column tops can have different heights; resolve actual text
      // collisions instead of assuming alternating offsets will be sufficient.
      while (placedTotals.some(prior => box.left < prior.right + 2 &&
        box.right + 2 > prior.left && box.top < prior.bottom + 2 && box.bottom + 2 > prior.top)) {
        y -= 14;
        label.setAttribute('y', y);
        box = label.getBoundingClientRect();
      }
      placedTotals.push(box);
    });
    labels.forEach((label, index) => {
      label.style.display = index % stride === 0 ? 'inline' : 'none';
      const text = label.querySelector('text');
      text.setAttribute('text-anchor', 'middle');
      const halfText = text.getComputedTextLength() / 2 + 1;
      text.setAttribute('x', Math.max(halfText, Math.min(width - halfText, Number(label.dataset.cx) * width)));
    });
  };
  resize();
  new ResizeObserver(resize).observe(viewport);
});

(() => {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const toggle = document.getElementById('navigation-toggle');
  const opener = document.getElementById('navigation-open');
  const closer = document.getElementById('navigation-close');
  const overlay = document.getElementById('navigation-overlay');
  const content = document.getElementById('page-content');
  const mobile = window.matchMedia('(max-width: 719.98px)');
  let collapsed = false;
  let open = false;
  try { collapsed = localStorage.getItem('qwennote.navigation.collapsed') === 'true'; } catch (_) {}
  function render() {
    const compact = collapsed && !mobile.matches;
    document.body.classList.toggle('navigation-collapsed', compact);
    document.body.classList.toggle('navigation-drawer-open', mobile.matches && open);
    toggle.setAttribute('aria-expanded', String(!compact));
    toggle.setAttribute('aria-label', compact ? '展开导航' : '收起导航');
    sidebar.querySelectorAll('.navigation-label').forEach(label => {
      label.textContent = compact ? '' : label.dataset.label;
    });
    opener.setAttribute('aria-expanded', String(open));
    overlay.hidden = !mobile.matches || !open;
    sidebar.inert = mobile.matches && !open;
    content.inert = mobile.matches && open;
    if (mobile.matches && open) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
    }
  }
  function close() { open = false; render(); opener.focus(); }
  toggle.addEventListener('click', () => {
    collapsed = !collapsed;
    try { localStorage.setItem('qwennote.navigation.collapsed', String(collapsed)); } catch (_) {}
    render();
  });
  opener.addEventListener('click', () => { open = true; render(); closer.focus(); });
  closer.addEventListener('click', close);
  overlay.addEventListener('click', close);
  document.addEventListener('keydown', event => {
    if (!mobile.matches || !open) return;
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const items = [...sidebar.querySelectorAll('a, button')].filter(el => el.getClientRects().length);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  mobile.addEventListener('change', () => { open = false; render(); });
  render();
})();

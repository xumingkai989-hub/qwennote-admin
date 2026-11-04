/* Page-local mobile controls. Existing forms keep their listeners and CSRF fields. */
(() => {
  'use strict';
  const mobile = matchMedia('(max-width: 719.98px)');
  let sequence = 0;
  const sheets = [];
  const button = (text, action) => {
    const item = document.createElement('button');
    item.type = 'button'; item.textContent = text;
    item.addEventListener('click', action);
    return item;
  };
  function sheet(owner, label, nodes, build) {
    const trigger = button('⋯', () => {
      reset(); dialog.showModal(); trigger.setAttribute('aria-expanded', 'true');
    });
    trigger.className = 'mobile-only more-button';
    trigger.setAttribute('aria-label', label + '的更多操作');
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-expanded', 'false');
    const dialog = document.createElement('dialog');
    dialog.className = 'mobile-sheet'; dialog.id = 'mobile-sheet-' + (++sequence);
    trigger.setAttribute('aria-controls', dialog.id);
    const heading = document.createElement('h2'); heading.id = dialog.id + '-title'; heading.textContent = label;
    dialog.setAttribute('aria-labelledby', heading.id);
    const content = document.createElement('div'); content.className = 'sheet-content';
    const cancel = button('关闭', () => dialog.close()); cancel.className = 'secondary sheet-close';
    dialog.append(heading, content, cancel); owner.append(trigger, dialog);
    // Placeholders let responsive changes restore the exact desktop structure.
    const homes = nodes.map(node => { const home = document.createComment('mobile action'); node.before(home); return home; });
    let reset = () => {};
    if (build) reset = build(content, nodes, dialog) || reset;
    const update = () => {
      if (dialog.open) dialog.close();
      if (mobile.matches) {
        if (!build) nodes.forEach(node => content.append(node));
        else reset();
      } else {
        if (build) reset();
        nodes.forEach((node, i) => homes[i].after(node));
      }
    };
    dialog.addEventListener('close', () => {
      trigger.setAttribute('aria-expanded', 'false');
      if (mobile.matches) trigger.focus({preventScroll:true});
    });
    dialog.addEventListener('click', event => {
      const rect = dialog.getBoundingClientRect();
      if (event.target === dialog && (event.clientY < rect.top || event.clientY > rect.bottom || event.clientX < rect.left || event.clientX > rect.right)) dialog.close();
    });
    // Native modal dialog supplies Escape, focus containment and background inertness.
    sheets.push({update, dialog}); update();
    return dialog;
  }
  const sync = document.querySelector('.sync-actions');
  if (sync) {
    const full = document.getElementById('full-sync-button');
    sheet(sync, '同步', [full]);
    full.addEventListener('click', () => full.closest('dialog')?.close());
  }
  document.querySelectorAll('.category-title').forEach(title => {
    sheet(title, title.querySelector('.category-name').textContent, [title.querySelector('.category-status')]);
  });
  document.querySelectorAll('.tag-row').forEach(row => {
    const category = row.querySelector('.tag-category-form');
    const actions = row.querySelector('.tag-actions');
    sheet(row, row.querySelector('.tag-name').textContent, [category, actions], (content, nodes) => {
      const menu = document.createElement('div'); menu.className = 'sheet-menu';
      const panel = document.createElement('div'); panel.className = 'sheet-panel';
      const back = button('返回操作', () => reset()); back.className = 'secondary';
      const status = document.createElement('p'); status.className = 'sheet-status'; status.setAttribute('role','status');
      const select = category.querySelector('select');
      function chooseCategory() {
        menu.hidden = true; panel.hidden = false; panel.replaceChildren(back);
        const choices = document.createElement('div'); choices.className = 'sheet-choices'; choices.setAttribute('role', 'group'); choices.setAttribute('aria-label', '修改分类');
        [...select.options].forEach(option => {
          const choice = button(option.textContent, async () => {
            select.value = option.value;
            select.dispatchEvent(new Event('change', {bubbles:true}));
            choices.querySelectorAll('button').forEach(b => b.disabled = select.disabled);
          });
          choice.setAttribute('aria-pressed', String(option.value === select.value));
          choice.dataset.value = option.value;
          choices.append(choice);
        });
        panel.append(choices, status);
        choices.querySelector('button').focus();
      }
      new MutationObserver(() => {
        status.textContent = category.querySelector('.category-save-status').textContent;
        panel.querySelectorAll('.sheet-choices button').forEach(b => {b.disabled = select.disabled; b.setAttribute('aria-pressed', String(b.dataset.value === select.value));});
      }).observe(category, {subtree:true, childList:true, attributes:true, attributeFilter:['disabled']});
      const merge = actions.querySelector('.merge-form');
      const deactivate = actions.querySelector('form:last-child');
      menu.append(button('修改分类', chooseCategory), button('合并到…', () => {
        menu.hidden = true; panel.hidden = false; panel.replaceChildren(back, merge); merge.querySelector('select').focus();
      }), button('停用', () => deactivate.requestSubmit()));
      content.append(menu, panel);
      function reset() {
        // Keep the original form within its tag row for optimistic row movement.
        content.append(category, actions); category.classList.add('sheet-source'); actions.classList.add('sheet-source');
        if (!actions.contains(merge)) actions.prepend(merge);
        menu.hidden = false; panel.hidden = true;
      }
      return reset;
    });
  });
  function mirrorStatus(dialog, id) {
    const source = document.getElementById(id);
    if (!source) return;
    const status = document.createElement('p'); status.className = 'sheet-status'; status.setAttribute('role','status');
    dialog.querySelector('.sheet-content').append(status);
    new MutationObserver(() => {if (dialog.open) status.textContent = source.textContent;}).observe(source, {childList:true,subtree:true,characterData:true});
  }
  document.querySelectorAll('.hotwords-table tbody tr').forEach(row => {
    const cell = row.lastElementChild;
    const dialog = sheet(cell, row.querySelector('th').childNodes[0].textContent, [cell.querySelector('.tag-actions')]);
    mirrorStatus(dialog, 'qwen-feedback');
    if (row.querySelector('details[open]') && mobile.matches) cell.querySelector('.more-button').click();
  });
  document.querySelectorAll('.model-actions').forEach(cell => {
    const dialog = sheet(cell, cell.closest('tr').querySelector('.model-name').textContent, [...cell.querySelectorAll('form')]);
    mirrorStatus(dialog, 'model-feedback');
  });
  mobile.addEventListener('change', () => sheets.forEach(s => s.update()));

  const header = document.querySelector('.detail-header');
  if (header) new ResizeObserver(() => {
    document.documentElement.style.setProperty('--detail-header-height', header.getBoundingClientRect().height + 'px');
  }).observe(header);
  const searchToggle = document.getElementById('transcript-search-toggle');
  if (searchToggle) searchToggle.addEventListener('click', () => {
    const open = searchToggle.getAttribute('aria-expanded') !== 'true';
    searchToggle.setAttribute('aria-expanded', String(open));
    document.getElementById('transcript-toolbar').classList.toggle('search-open', open);
    const input = document.getElementById('transcript-search');
    if (open) input.focus();
    else {input.value = ''; input.dispatchEvent(new Event('input'));}
  });
  const summary = document.getElementById('mobile-sync-result');
  if (summary) summary.addEventListener('click', event => {
    const control = event.target.closest('button'); if (!control) return;
    const open = control.getAttribute('aria-expanded') !== 'true';
    control.setAttribute('aria-expanded', String(open));
    document.getElementById('sync-error').classList.toggle('mobile-error-open', open);
  });
  window.mobileDensity = {
    syncPending(full) {
      if (summary) summary.textContent = full ? '正在全量扫描…' : '正在快速同步…';
      document.getElementById('sync-error')?.classList.remove('mobile-error-open');
    },
    syncResult(result) {
      if (!summary) return;
      const time = result.started_at?.slice(11,16) || new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date());
      if (result.status === 'failed') {
        const detail = button(`${time} 同步失败 · 查看详情`, () => {});
        detail.className = 'text-button'; detail.setAttribute('aria-expanded', 'false'); detail.setAttribute('aria-controls','sync-error'); summary.replaceChildren(detail);
      } else summary.textContent = `${time} ${result.mode === 'full' ? '全盘同步' : '快速同步'} · ${result.inserted ? `新增 ${result.inserted} 条` : result.updated ? `更新 ${result.updated} 条` : '无变化'}`;
    }
  };
})();

(function () {
  'use strict';
  const pageSize = 5;
  const groups = [
    ['#conversation-list', '.conversation'],
    ['#sources-list', '.source-card'],
    ['#subview .app-list', '.app-list-item'],
    ['#subview .app-version-list', '.app-version-chip'],
    ['#subview .app-knowledge-list', 'label'],
    ['#subview .app-check-grid', '.app-check,.app-source-card'],
    ['#subview .knowledge-connections', '.knowledge-connection'],
    ['#subview .result-list', '.result-item'],
    ['#subview .workflow-run-log', 'div'],
    ['#subview .rp-list', 'button'],
    ['#subview .sp-list', '.sp-list-item'],
    ['#subview .mc-profile-list', '.mc-profile-item'],
    ['#subview .dc-dictionary-list', '.dc-dictionary-item'],
    ['#subview #dc-test-result', '.dc-hit'],
    ['#subview .eval-agent-grid', '.eval-agent-card'],
    ['#subview .eval-history-list', '.eval-history-item'],
    ['#subview .eval-case-list', '.eval-case-detail'],
    ['#subview .card-grid', '.info-card'],
    ['#subview table:is(.data-table,.review-table,.mc-table,.dc-entry-table,.dc-compare-table,.eval-ranking-table)>tbody', 'tr'],
  ];
  const states = new WeakMap();
  const pages = new Map();

  function identity(row) {
    const keys = [...row.attributes].filter(item => item.name.startsWith('data-'));
    return keys.length ? keys.map(item => item.name + '=' + item.value).join('|') : row.textContent;
  }
  function update(container, itemSelector, key) {
    const rows = [...container.children].filter(row => row.matches(itemSelector));
    const eligible = rows.filter(row => !row.hidden && row.style.display !== 'none');
    const signature = eligible.map(identity).join('\u001f');
    let state = states.get(container);
    if (!state) {
      const saved = pages.get(key);
      state = { page: saved?.signature === signature ? saved.page : 1, signature, pager: null };
      states.set(container, state);
    } else if (state.signature !== signature) {
      state.page = 1;
      state.signature = signature;
    }
    const currentRows = new Set(rows);
    (state.rows || []).forEach(row => { if (!currentRows.has(row)) row.classList.remove('list-page-hidden'); });
    state.rows = rows;
    const count = Math.max(1, Math.ceil(eligible.length / pageSize));
    state.page = Math.min(Math.max(1, state.page), count);
    pages.set(key, { page: state.page, signature });
    const start = (state.page - 1) * pageSize;
    const visible = new Set(eligible.slice(start, start + pageSize));
    const matched = new Set(eligible);
    rows.forEach(row => row.classList.toggle('list-page-hidden', matched.has(row) && !visible.has(row)));
    if (!state.pager && eligible.length <= pageSize) return;
    if (!state.pager) {
      const pager = document.createElement('nav');
      pager.className = 'list-pagination';
      pager.setAttribute('aria-label', '列表分页');
      pager.innerHTML = '<span class="list-pagination-count" aria-live="polite"></span><div><button type="button" data-list-page="prev">上一页</button><button type="button" data-list-page="next">下一页</button></div>';
      pager.addEventListener('click', event => {
        const button = event.target.closest('[data-list-page]');
        if (!button || button.disabled) return;
        event.preventDefault();event.stopPropagation();
        state.page += button.dataset.listPage === 'next' ? 1 : -1;
        update(container, itemSelector, key);
      });
      state.pager = pager;
    }
    const table = container.tagName === 'TBODY' ? container.closest('table') : null;
    const anchor = table ? (table.parentElement.children.length === 1 ? table.parentElement : table) : container;
    if (state.pager.previousElementSibling !== anchor) anchor.after(state.pager);
    const hidden = eligible.length <= pageSize;
    if (state.pager.hidden !== hidden) state.pager.hidden = hidden;
    const label = '第 ' + state.page + ' / ' + count + ' 页 · 共 ' + eligible.length + ' 条 · 每页 ' + pageSize + ' 条';
    const text = state.pager.querySelector('.list-pagination-count');
    if (text.textContent !== label) text.textContent = label;
    state.pager.querySelector('[data-list-page="prev"]').disabled = state.page === 1;
    state.pager.querySelector('[data-list-page="next"]').disabled = state.page === count;
  }
  function refresh() {
    const view = document.querySelector('.nav [data-view].active')?.dataset.view || 'chat';
    groups.forEach(([selector, items]) => {
      document.querySelectorAll(selector).forEach((container, index) => update(container, items, view + ':' + selector + ':' + index));
    });
  }
  let pending = false;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    queueMicrotask(() => { pending = false;refresh(); });
  });
  ['subview', 'conversation-list', 'sources-list'].forEach(id => {
    const root = document.getElementById(id);
    if (root) observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'hidden'] });
  });
  window.ListPagination = { refresh, pageSize };
  refresh();
})();

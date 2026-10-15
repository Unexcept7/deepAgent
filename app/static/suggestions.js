(function () {
  'use strict';

  function normalizeSuggestions(items) {
    if (!Array.isArray(items)) return [];
    const seen = new Set();
    const suggestions = [];
    for (const item of items) {
      const query = typeof item === 'string'
        ? item.trim()
        : String(item?.query || item?.text || '').trim();
      if (!query || seen.has(query)) continue;
      seen.add(query);
      const text = typeof item === 'string' ? query : String(item?.text || query).trim();
      suggestions.push({ text: text || query, query });
      if (suggestions.length === 4) break;
    }
    return suggestions;
  }

  function setBusy(root, busy) {
    if (!root) return;
    root.querySelectorAll('.answer-suggestions button').forEach(button => {
      button.disabled = Boolean(busy);
    });
  }

  function render(result, bubble, { onSelect, isBusy = () => false } = {}) {
    if (!bubble) return;
    bubble.querySelectorAll('.answer-suggestions').forEach(node => node.remove());
    const suggestions = normalizeSuggestions(result?.suggestions);
    if (!suggestions.length || typeof onSelect !== 'function') return;

    const group = document.createElement('section');
    group.className = 'answer-suggestions';
    group.setAttribute('aria-label', '推荐提问');
    const heading = document.createElement('div');
    heading.className = 'answer-suggestions-heading';
    const label = document.createElement('span');
    label.textContent = result?.evidence?.status === 'context_ambiguous'
      ? '请选择查询对象' : '相关信息';
    const hint = document.createElement('span');
    hint.className = 'answer-suggestions-hint';
    hint.textContent = '点击直接提问';
    heading.appendChild(label);
    heading.appendChild(hint);
    group.appendChild(heading);

    const choices = document.createElement('div');
    choices.className = 'answer-suggestions-choices';
    let submitting = false;
    suggestions.forEach(item => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'answer-suggestion';
      button.textContent = item.text;
      button.title = `直接提问：${item.query}`;
      button.disabled = Boolean(isBusy());
      button.addEventListener('click', async () => {
        if (submitting || isBusy()) return;
        submitting = true;
        setBusy(group, true);
        try {
          await onSelect(item.query);
        } finally {
          submitting = false;
          setBusy(group, isBusy());
        }
      });
      choices.appendChild(button);
    });
    group.appendChild(choices);
    bubble.insertBefore(group, bubble.querySelector('.answer-meta'));
  }

  window.DeepAgentSuggestions = { render, setBusy };
})();

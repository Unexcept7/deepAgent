(function () {
  'use strict';

  const state = { run: null, resultFilter: 'all' };

  const esc = value => String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  async function json(url, options) {
    const response = await fetch(url, options);
    let data = {};
    try { data = await response.json(); } catch (_) { /* 状态码信息足够 */ }
    if (!response.ok) throw new Error(data.detail || data.message || `请求失败：HTTP ${response.status}`);
    return data;
  }

  function formatNumber(value) {
    if (value === null || value === undefined || value === '') return '—';
    return Number(value).toLocaleString('zh-CN');
  }

  function formatPercent(value) {
    return value === null || value === undefined ? '仅观察' : `${(Number(value) * 100).toFixed(1)}%`;
  }

  function formatDuration(value) {
    const number = Number(value || 0);
    return number >= 1000 ? `${(number / 1000).toFixed(2)} s` : `${number} ms`;
  }

  function status(value) {
    const labels = { ready: '可评测', unavailable: '配置不可用', success: '已完成', error: '有失败' };
    const good = value === 'ready' || value === 'success';
    return `<span class="eval-pill ${good ? 'ok' : value === 'error' || value === 'unavailable' ? 'danger' : 'muted'}"><i></i>${esc(labels[value] || value || '未知')}</span>`;
  }

  function shell(body, actions) {
    return `<div class="eval-page-head">
      <div><div class="eval-eyebrow">质量治理 · Agent 评测</div><h1>评测中心</h1><p>用同一套业务问题和评分维度，对比已发布 Agent 的回答、证据、检索和运行成本。</p></div>
      <div class="eval-page-actions">${actions || ''}</div>
    </div>${body}`;
  }

  function parseCases(raw) {
    const text = String(raw || '').trim();
    if (!text) throw new Error('请至少输入一个问题，或先载入一个评测集');
    if (text.startsWith('[')) {
      const parsed = JSON.parse(text);
      if (!Array.isArray(parsed)) throw new Error('评测用例 JSON 必须是数组');
      return parsed.map((item, index) => {
        if (typeof item === 'string') return { query: item, case_id: `case-${index + 1}` };
        if (!item || !String(item.query || '').trim()) throw new Error(`第 ${index + 1} 条用例缺少 query`);
        return item;
      });
    }
    if (text.startsWith('{')) {
      const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      try {
        return lines.map(line => JSON.parse(line));
      } catch (error) {
        throw new Error(`JSONL 问题集格式不正确：${error.message}`);
      }
    }
    return text.split(/\r?\n/).map((query, index) => ({
      query: query.trim(), case_id: `case-${index + 1}`,
    })).filter(item => item.query);
  }

  function agentCard(item, index) {
    const ready = item.status === 'ready';
    const sourceText = (item.source_types || []).join('、') || '默认来源';
    return `<label class="eval-agent-card ${ready ? '' : 'is-disabled'}">
      <input type="checkbox" data-eval-agent="${esc(item.app_id)}" ${ready && index < 2 ? 'checked' : ''} ${ready ? '' : 'disabled'}>
      <span class="eval-agent-card-check"></span>
      <span class="eval-agent-card-body">
        <span class="eval-agent-card-title"><strong>${esc(item.name || item.app_id)}</strong>${status(item.status)}</span>
        <span class="eval-agent-card-meta">v${esc(item.version || '-')} · ${esc(item.workflow_name || item.workflow_id || '未绑定工作流')}</span>
        <span class="eval-agent-card-desc">${esc(item.description || '使用该 Agent 的已发布工作流和来源范围进行评测。')}</span>
        <span class="eval-agent-card-tags"><b>${esc(sourceText)}</b><b>发布版本</b></span>
        ${!ready ? `<span class="eval-agent-error">${esc(item.error || '发布配置不可运行')}</span>` : ''}
      </span>
    </label>`;
  }

  function datasetOptions(datasets) {
    const ready = (datasets || []).filter(item => item.status === 'ready');
    if (!ready.length) return '<option value="">暂无可用内置问题集</option>';
    return `<option value="">不加载内置问题集</option>${ready.map(item => `<option value="${esc(item.dataset_id)}">${esc(item.name || item.dataset_id)} · ${esc(item.case_count || 0)} 条</option>`).join('')}`;
  }

  function renderKpis(agents, datasets, runs) {
    const latest = (runs || [])[0];
    const summary = latest?.summary || {};
    return `<div class="eval-kpi-grid">
      <div class="eval-kpi"><span>可评测 Agent</span><strong>${formatNumber((agents || []).filter(item => item.status === 'ready').length)}</strong><small>已发布且工作流可运行</small></div>
      <div class="eval-kpi"><span>可复用评测集</span><strong>${formatNumber((datasets || []).filter(item => item.status === 'ready').length)}</strong><small>检索金标与业务问题集</small></div>
      <div class="eval-kpi"><span>最近任务通过率</span><strong>${formatPercent(summary.pass_rate)}</strong><small>${latest ? esc(latest.name || '最近一次评测') : '完成一次评测后显示'}</small></div>
      <div class="eval-kpi"><span>最近任务样本数</span><strong>${formatNumber(summary.total_runs || 0)}</strong><small>${latest ? 'Agent × 用例的运行总数' : '尚无评测记录'}</small></div>
    </div>`;
  }

  function renderHistory(runs) {
    if (!(runs || []).length) return '<div class="eval-empty-small"><strong>还没有评测任务</strong><span>完成第一次评测后，历史任务会保留在这里。</span></div>';
    return (runs || []).map(item => `<button class="eval-history-item" type="button" data-action="eval-load" data-eval-run="${esc(item.run_id || '')}">
      <span class="eval-history-main"><strong>${esc(item.name || '未命名评测')}</strong><small>${esc(item.created_at || '')}</small></span>
      <span class="eval-history-side">${formatPercent(item.summary?.pass_rate)}<small>${formatNumber(item.result_count || 0)} 次运行</small></span>
    </button>`).join('');
  }

  function checkLabel(type) {
    return ({
      answer_contains: '答案命中', intent: '意图', entity: '实体', source_type: '来源',
      document: '文档', concept: '关键概念', outcome: '结果类型', evidence_status: '证据状态',
    })[type] || type;
  }

  function renderChecks(item) {
    const checks = item.checks || [];
    if (!checks.length) return '<span class="eval-observed-note">未设置期望值，仅记录运行结果</span>';
    return `<div class="eval-check-list">${checks.map(check => `<span class="eval-check ${check.passed ? 'pass' : 'fail'}"><i>${check.passed ? '✓' : '×'}</i>${esc(checkLabel(check.type))}：${esc(check.expected)}</span>`).join('')}</div>`;
  }

  function renderRun(run) {
    const summary = run.summary || {};
    const rankings = summary.rankings || [];
    const filter = state.resultFilter || 'all';
    const allResults = run.results || [];
    const results = allResults.filter(item => filter === 'failed' ? item.passed === false : filter === 'observed' ? item.passed === null : true);
    const rows = rankings.length ? rankings.map(item => `<tr>
      <td><span class="eval-rank ${Number(item.rank) === 1 ? 'first' : ''}">${esc(item.rank || '-')}</span></td>
      <td><strong>${esc(item.agent_name || item.agent_id)}</strong><small>v${esc(item.version || item.agent_version || '-')} · ${formatNumber(item.case_count || 0)} 条</small></td>
      <td><b class="${item.average_score === null ? 'eval-text-muted' : ''}">${formatPercent(item.average_score)}</b></td>
      <td>${item.pass_rate === null ? '—' : formatPercent(item.pass_rate)}</td>
      <td>${formatDuration(item.average_duration_ms)}</td>
      <td>${formatNumber(item.total_tokens)}</td>
      <td>${item.evidence_supported_rate === null ? '—' : formatPercent(item.evidence_supported_rate)}</td>
    </tr>`).join('') : '<tr><td colspan="7" class="eval-table-empty">暂无可汇总结果</td></tr>';
    const details = results.map(item => `<details class="eval-case-detail ${item.passed === false ? 'is-failed' : ''}">
      <summary><span class="eval-case-summary-title"><b>${esc(item.agent_name || item.agent_id)}</b><small>${esc(item.case_id)}</small></span><span class="eval-case-summary-status ${item.passed === true ? 'pass' : item.passed === false ? 'fail' : 'observe'}">${item.passed === true ? '通过' : item.passed === false ? '未通过' : '仅观察'}</span><span>${formatDuration(item.duration_ms)}</span></summary>
      <div class="eval-case-body"><div class="eval-case-question"><span>问题</span><p>${esc(item.query)}</p></div><div class="eval-case-answer"><span>回答</span><p>${esc(item.answer || item.error || '无')}</p></div>${renderChecks(item)}<div class="eval-case-meta"><span>意图：${esc((item.intents || []).join('、') || '未识别')}</span><span>实体：${esc((item.entities || []).join('、') || '未识别')}</span><span>来源：${esc((item.source_types || []).join('、') || '无')}</span><span>文档：${esc((item.documents || []).join('、') || '无')}</span><span>证据：${esc(item.evidence_status || '未返回')}</span><span>Token：${formatNumber(item.total_tokens)}</span></div></div>
    </details>`).join('');
    return `<section class="eval-report-card"><div class="eval-report-head"><div><div class="eval-eyebrow">评测报告</div><h2>${esc(run.name || '评测结果')}</h2><p>${esc(run.run_id || '')} · ${esc(run.completed_at || '')} · ${formatNumber(summary.total_runs || 0)} 次 Agent 用例</p></div><span class="eval-report-status">${status(run.status)}</span></div>
      <div class="eval-report-kpis"><div><span>综合通过率</span><strong>${formatPercent(summary.pass_rate)}</strong><small>${formatNumber(summary.passed_runs || 0)} / ${formatNumber(summary.scored_runs || 0)} 条通过</small></div><div><span>评测对象</span><strong>${formatNumber(summary.agent_count || 0)}</strong><small>已发布 Agent</small></div><div><span>问题数量</span><strong>${formatNumber(summary.case_count || 0)}</strong><small>相同问题集横向比较</small></div><div><span>失败样本</span><strong>${formatNumber(allResults.filter(item => item.passed === false).length)}</strong><small>优先复核这些用例</small></div></div>
      <div class="eval-report-section"><div class="eval-section-heading"><div><h3>Agent 排名</h3><p>按综合得分排序；仅观察任务不会伪造通过率。</p></div><span class="eval-report-filter"><label for="eval-result-filter">查看</label><select id="eval-result-filter"><option value="all" ${filter === 'all' ? 'selected' : ''}>全部样本</option><option value="failed" ${filter === 'failed' ? 'selected' : ''}>只看未通过</option><option value="observed" ${filter === 'observed' ? 'selected' : ''}>只看仅观察</option></select></span></div><div class="eval-table-scroll"><table class="eval-ranking-table"><thead><tr><th>排名</th><th>Agent</th><th>平均得分</th><th>通过率</th><th>平均耗时</th><th>Token</th><th>证据支持率</th></tr></thead><tbody>${rows}</tbody></table></div></div>
      <div class="eval-report-section"><div class="eval-section-heading"><div><h3>逐题分析</h3><p>展开样本查看回答、证据、文档命中和每个评分项。</p></div><span class="eval-result-count">显示 ${formatNumber(results.length)} / ${formatNumber(allResults.length)} 条</span></div><div class="eval-case-list">${details || '<div class="eval-empty-small">当前筛选条件下没有样本。</div>'}</div></div>
    </section>`;
  }

  async function renderEvaluationView() {
    const [agentData, runData, datasetData] = await Promise.all([
      json('/api/evaluations/agents'),
      json('/api/evaluations?limit=50'),
      json('/api/evaluations/datasets'),
    ]);
    const agents = agentData.agents || [];
    const datasets = datasetData.datasets || [];
    const runs = runData.runs || [];
    const readyCount = agents.filter(item => item.status === 'ready').length;
    const body = `<div class="eval-page">
      ${renderKpis(agents, datasets, runs)}
      <div class="eval-workspace-grid"><section class="eval-card eval-config-card">
        <div class="eval-card-head"><div><div class="eval-eyebrow">新建任务</div><h2>创建一次对比评测</h2><p>先选 Agent，再选择问题集和验证维度；配置完成后统一执行。</p></div><span class="eval-capacity">${formatNumber(readyCount)} 个 Agent 可用</span></div>
        <div class="eval-step"><span class="eval-step-number">01</span><div class="eval-step-content"><div class="eval-section-heading"><div><h3>选择评测对象</h3><p>只展示已发布版本，确保结果可复现。</p></div><span class="eval-selection-hint">建议至少选择 2 个</span></div><div class="eval-agent-grid">${agents.length ? agents.map(agentCard).join('') : '<div class="eval-empty-small">暂无已发布 Agent，请先在应用中心发布应用。</div>'}</div></div></div>
        <div class="eval-step"><span class="eval-step-number">02</span><div class="eval-step-content"><div class="eval-section-heading"><div><h3>选择评测集</h3><p>可复用项目已有检索金标集，也可以手工粘贴问题。</p></div><span class="eval-selection-hint">RAGFlow 仍负责文档治理</span></div><div class="eval-dataset-row"><select id="eval-dataset-select">${datasetOptions(datasets)}</select><input id="eval-dataset-split" placeholder="split（可选，如 dev）"><button class="eval-button secondary" type="button" data-action="eval-load-dataset">载入问题集</button></div><div class="eval-helper">内置问题集只提供问题、期望文档和关键概念，不复制 RAGFlow 文档内容；载入后仍可继续编辑。</div><label class="eval-field-label" for="eval-cases">问题集内容</label><textarea id="eval-cases" rows="7" placeholder="每行一个问题；也支持 JSON 数组或 JSONL。&#10;&#10;例如：&#10;M1401 模块的通信接口是什么？&#10;M1401 模块有多少路模拟量输出？"></textarea></div></div>
        <div class="eval-step"><span class="eval-step-number">03</span><div class="eval-step-content"><div class="eval-section-heading"><div><h3>评测维度</h3><p>期望值写在每条用例里，系统按已有数据自动核验。</p></div><span class="eval-selection-hint">规则核验 · 可审计</span></div><div class="eval-dimension-grid"><span><i>✓</i>答案命中</span><span><i>✓</i>意图识别</span><span><i>✓</i>实体识别</span><span><i>✓</i>来源类型</span><span><i>✓</i>文档命中</span><span><i>✓</i>证据状态</span></div><div class="eval-helper">不填写期望值时为“观察评测”，只记录答案、证据、耗时和 Token；填写期望值后才计算得分与通过率。</div></div></div>
        <div class="eval-submit-bar"><div><label class="eval-field-label" for="eval-name">评测任务名称</label><input id="eval-name" value="多智能体对比评测" maxlength="160"></div><div class="eval-submit-actions"><span id="eval-run-status" class="eval-run-status">每个 Agent 使用自己的已发布工作流和来源范围。</span><button class="eval-button primary" type="button" data-action="eval-run">开始评测</button></div></div>
      </section><aside class="eval-side-column"><section class="eval-card eval-history-card"><div class="eval-card-head compact"><div><div class="eval-eyebrow">任务记录</div><h2>最近评测</h2></div><span class="eval-capacity">保留 50 次</span></div><div class="eval-history-list">${renderHistory(runs)}</div></section><section class="eval-card eval-method-card"><div class="eval-eyebrow">评测方法</div><h2>先看结果，再下钻样本</h2><div class="eval-method-item"><b>01</b><span><strong>横向排行榜</strong><small>同一问题集、同一执行条件，对比 Agent 的得分和成本。</small></span></div><div class="eval-method-item"><b>02</b><span><strong>证据质量</strong><small>检查来源、文档命中和证据状态，不只看回答文字。</small></span></div><div class="eval-method-item"><b>03</b><span><strong>失败样本</strong><small>按未通过筛选，展开查看问题、回答和具体失败维度。</small></span></div></section></aside></div>
      <div id="evaluation-results" class="eval-results"><div class="eval-empty-report"><div class="eval-empty-icon">◎</div><strong>还没有评测报告</strong><span>选择 Agent 和问题集后开始；报告会先展示排名，再提供逐题分析。</span></div></div>
    </div>`;
    return shell(body, '<button class="eval-button secondary" type="button" data-action="refresh-view">刷新数据</button>');
  }

  async function runEvaluation() {
    const agentIds = [...document.querySelectorAll('input[data-eval-agent]:checked')].map(item => item.dataset.evalAgent).filter(Boolean);
    if (!agentIds.length) throw new Error('请至少选择一个已发布 Agent');
    const cases = parseCases(document.querySelector('#eval-cases')?.value);
    const statusNode = document.querySelector('#eval-run-status');
    const target = document.querySelector('#evaluation-results');
    if (statusNode) statusNode.textContent = `正在执行 ${agentIds.length} 个 Agent × ${cases.length} 条用例…`;
    if (target) target.innerHTML = '<div class="eval-running"><span class="eval-spinner"></span><div><strong>评测正在执行</strong><span>系统会逐个运行 Agent，并记录答案、证据、耗时和 Token；期间不会写入用户会话。</span></div></div>';
    try {
      const run = await json('/api/evaluations/run', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: document.querySelector('#eval-name')?.value.trim() || '未命名评测', agent_ids: agentIds, cases, max_parallel: 2 }),
      });
      state.run = run;
      state.resultFilter = 'all';
      if (target) target.innerHTML = renderRun(run);
      if (statusNode) statusNode.textContent = `已完成：${run.summary?.total_runs || 0} 次运行`;
    } catch (error) {
      if (statusNode) statusNode.textContent = '评测失败，请检查 Agent 发布状态和数据源连接。';
      if (target) target.innerHTML = `<div class="eval-error"><strong>评测未完成</strong><span>${esc(error.message)}</span></div>`;
      throw error;
    }
  }

  async function loadEvaluationRun(runId) {
    if (!runId) return;
    const run = await json(`/api/evaluations/${encodeURIComponent(runId)}`);
    state.run = run;
    state.resultFilter = 'all';
    const target = document.querySelector('#evaluation-results');
    if (target) target.innerHTML = renderRun(run);
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function loadEvaluationDataset() {
    const dataset = document.querySelector('#eval-dataset-select')?.value;
    if (!dataset) throw new Error('请先选择一个内置问题集');
    const split = document.querySelector('#eval-dataset-split')?.value.trim();
    const params = new URLSearchParams({ limit: '100' });
    if (split) params.set('split', split);
    const data = await json(`/api/evaluations/datasets/${encodeURIComponent(dataset)}?${params.toString()}`);
    const textarea = document.querySelector('#eval-cases');
    if (textarea) textarea.value = JSON.stringify(data.cases || [], null, 2);
    const statusNode = document.querySelector('#eval-run-status');
    if (statusNode) statusNode.textContent = `已载入 ${data.total || 0} 条问题，可直接开始评测或继续编辑。`;
  }

  document.addEventListener('change', event => {
    if (event.target?.id !== 'eval-result-filter' || !state.run) return;
    state.resultFilter = event.target.value || 'all';
    const target = document.querySelector('#evaluation-results');
    if (target) target.innerHTML = renderRun(state.run);
  });

  window.renderEvaluationView = renderEvaluationView;
  window.runEvaluation = runEvaluation;
  window.loadEvaluationRun = loadEvaluationRun;
  window.loadEvaluationDataset = loadEvaluationDataset;
}());

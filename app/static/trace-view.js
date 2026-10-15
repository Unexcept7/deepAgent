(function () {
  'use strict';

  const esc = window.escapeHtml || (value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch])));
  const typeLabels = {
    stage: '阶段', llm: '大模型', retriever: '检索', tool: '工具', api: 'API',
    embedding: 'Embedding', guardrail: '安全校验', agent: 'Agent'
  };
  const componentLabels = {
    qwen: 'Qwen / DashScope', ragflow: 'RAGFlow', unified_search: '统一搜索',
    document: '文档知识库', qa: '标准 QA', graph: 'Neo4j 图谱', sql: '结构化数据',
    api: '业务 API', mcp: 'MCP', workflow: '工作流', tool_center: '工具中心', backend: '后端'
  };

  const style = document.createElement('style');
  style.textContent = `
    #trace-panel-dock { margin-top:10px; }
    .answer-trace-panel { margin-top:0; padding-top:7px; color:#667085; border-top:1px solid #edf0f3; }
    .answer-trace-summary { display:flex; align-items:center; gap:6px; min-width:0; font-size:10px; }
    .answer-trace-title { flex:0 0 auto; color:#475467; font-weight:600; }
    .answer-trace-summary #trace-summary { min-width:0; overflow:hidden; color:#98a2b3; font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
    .answer-trace-summary #run-state { flex:0 0 auto; padding:2px 5px; color:#667085; background:#f8fafc; border-radius:4px; font-size:8px; }
    .answer-observability-duration { flex:0 0 auto; margin-left:auto; color:#667085; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .answer-observability button { margin-left:0; }
    .answer-live-trace { width:100%; box-sizing:border-box; margin-top:8px; padding:8px 9px; color:#667085; background:#fbfdff; border:1px solid #e7edf4; border-radius:6px; }
    .answer-live-trace-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:5px; color:#475467; font-size:9px; font-weight:600; }
    .answer-live-trace-head span:last-child { color:#98a2b3; font-size:8px; font-weight:400; }
    .answer-live-stage-list { display:grid; gap:3px; }
    .answer-live-stage { display:flex; align-items:center; gap:6px; min-width:0; padding:4px 5px; border-radius:4px; font-size:9px; line-height:1.35; }
    .answer-live-stage.is-current { color:#175cd3; background:#eff8ff; }
    .answer-live-stage.is-error { color:#b42318; background:#fff1f0; }
    .answer-live-stage-dot { width:6px; height:6px; flex:0 0 6px; background:#b8c2cc; border-radius:50%; }
    .answer-live-stage.is-done .answer-live-stage-dot { background:#12b76a; }
    .answer-live-stage.is-current .answer-live-stage-dot { background:#2563eb; box-shadow:0 0 0 3px #dbeafe; }
    .answer-live-stage.is-error .answer-live-stage-dot { background:#f04438; }
    .answer-live-stage-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .answer-live-stage-state { margin-left:auto; flex:0 0 auto; color:#98a2b3; font-size:8px; white-space:nowrap; }
    .answer-live-stage.is-current .answer-live-stage-state { color:#175cd3; }
    .answer-live-stage.is-error .answer-live-stage-state { color:#b42318; }
    .trace-v2 { max-height:none; overflow:visible; margin-top:7px; padding:8px 0 2px; border-top:1px solid #f0f2f4; }
    .rail { min-height:0; }
    .trace-v2-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:7px; }
    .trace-v2-id { max-width:170px; overflow:hidden; color:#98a2b3; font:10px ui-monospace,SFMono-Regular,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-summary { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:4px; margin-bottom:7px; }
    .trace-v2-stat { min-width:0; padding:5px 6px; background:#f8fafc; border:1px solid #eef2f6; border-radius:5px; }
    .trace-v2-stat span { display:block; overflow:hidden; color:#667085; font-size:9px; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-stat b { display:block; margin-top:2px; overflow:hidden; color:#1d2939; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-list { display:grid; gap:3px; }
    .trace-v2-groups { display:grid; gap:6px; }
    .trace-v2-group { overflow:hidden; background:#fff; border:1px solid #e7edf4; border-radius:6px; }
    .trace-v2-group > summary { display:block; min-width:0; padding:7px 8px 8px; border-left:3px solid #98a2b3; cursor:pointer; list-style:none; }
    .trace-v2-group > summary::-webkit-details-marker { display:none; }
    .trace-v2-group > summary::after { content:'展开明细'; margin-left:auto; flex:0 0 auto; color:#98a2b3; font-size:8px; }
    .trace-v2-group[open] > summary { border-bottom:1px solid #edf0f3; background:#fbfcfe; }
    .trace-v2-group[open] > summary::after { content:'收起明细'; }
    .trace-v2-group-static-head { display:block; min-width:0; padding:7px 8px 8px; border-left:3px solid #98a2b3; background:#fbfcfe; }
    .trace-v2-group-static-answer .trace-v2-group-static-head { border-left-color:#7f56d9; }
    .trace-v2-group-static .trace-v2-group-summary-head { display:flex; align-items:center; gap:5px; min-width:0; }
    .trace-v2-group-single.retrieval > summary { border-left-color:#12b76a; }
    .trace-v2-group-single.answer > summary { border-left-color:#7f56d9; }
    .trace-v2-group-single.planning > summary { border-left-color:#f79009; }
    .trace-v2-group-single > summary .trace-v2-group-summary-head { display:flex; align-items:center; gap:5px; min-width:0; }
    .trace-v2-group-summary-detail { margin-top:5px; padding-top:5px; color:#667085; border-top:1px dashed #edf0f3; font-size:9px; line-height:1.45; word-break:break-word; }
    .trace-v2-group-title { min-width:0; overflow:hidden; color:#344054; font-size:10px; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-group-meta { display:flex; align-items:center; gap:3px; min-width:0; margin-left:auto; }
    .trace-v2-group-body { display:grid; gap:3px; padding:6px; }
    .trace-v2-group-detail { padding:0 2px 4px; color:#667085; font-size:9px; line-height:1.45; word-break:break-word; }
    .trace-v2-retrieval-summary { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:4px; padding:6px 7px; background:#f8fafc; border:1px solid #e7edf4; border-radius:5px; }
    .trace-v2-retrieval-summary span { display:block; color:#667085; font-size:8px; }
    .trace-v2-retrieval-summary b { display:block; margin-top:2px; color:#344054; font-size:10px; }
    .trace-v2-group-body .trace-v2-row { border-radius:4px; }
    .trace-v2-row { position:relative; min-width:0; padding:5px 7px 5px 8px; background:#fff; border:1px solid #e7edf4; border-left:3px solid #98a2b3; border-radius:5px; }
    .trace-v2-row.llm { border-left-color:#f79009; }.trace-v2-row.retriever { border-left-color:#12b76a; }.trace-v2-row.api { border-left-color:#7f56d9; }.trace-v2-row.tool { border-left-color:#2563eb; }.trace-v2-row.error { border-left-color:#f04438; background:#fffafa; }.trace-v2-row.running { border-left-color:#2563eb; background:#f8fbff; }
    .trace-v2-title { display:flex; align-items:center; gap:4px; min-width:0; }
    .trace-v2-title strong { min-width:0; overflow:hidden; color:#344054; font-size:10px; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-row-head { display:flex; align-items:center; gap:4px; min-width:0; }
    .trace-v2-kind { flex:0 0 auto; padding:1px 4px; color:#475467; background:#f2f4f7; border-radius:3px; font-size:8px; font-weight:700; letter-spacing:.2px; }.trace-v2-row.llm .trace-v2-kind { color:#9a5a00; background:#fff3dc; }.trace-v2-row.retriever .trace-v2-kind { color:#087443; background:#e5f7ee; }.trace-v2-row.api .trace-v2-kind { color:#6941c6; background:#f9f5ff; }.trace-v2-row.tool .trace-v2-kind { color:#175cd3; background:#eff8ff; }
    .trace-v2-row-head strong { min-width:0; overflow:hidden; color:#344054; font-size:10px; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-quick { max-width:100px; overflow:hidden; padding:1px 4px; color:#667085; background:#f8fafc; border-radius:3px; font-size:8px; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-row-head .trace-v2-chip { flex:0 0 auto; }
    .trace-v2-duration { margin-left:auto; flex:0 0 auto; color:#667085; font:9px ui-monospace,SFMono-Regular,Consolas,monospace; }
    .trace-v2-meta { display:flex; flex-wrap:wrap; gap:3px 5px; margin-top:3px; color:#667085; font-size:9px; line-height:1.35; }
    .trace-v2-chip { padding:1px 4px; background:#f8fafc; border-radius:3px; font-size:8px; }.trace-v2-chip.status-success { color:#087443; background:#ecfdf3; }.trace-v2-chip.status-error { color:#b42318; background:#fff1f0; }.trace-v2-chip.status-waiting_confirmation { color:#9a5a00; background:#fff7e6; }.trace-v2-chip.status-running { color:#175cd3; background:#eff8ff; }
    .trace-v2-detail { margin-top:5px; padding-top:5px; color:#667085; border-top:1px dashed #edf0f3; font-size:9px; line-height:1.45; word-break:break-word; }
    .trace-v2-error { margin-top:5px; padding:5px 6px; color:#b42318; background:#fff1f0; border-radius:4px; font-size:9px; line-height:1.4; word-break:break-word; }
    .trace-v2-note { margin-top:6px; color:#98a2b3; font-size:9px; line-height:1.4; }
    .trace-v2-routing { margin-bottom:7px; overflow:hidden; background:#fbfcfe; border:1px solid #e7edf4; border-radius:6px; }
    .trace-v2-routing > summary { display:flex; align-items:center; gap:5px; padding:7px 8px; color:#344054; cursor:pointer; list-style:none; font-size:10px; font-weight:700; }
    .trace-v2-routing > summary::-webkit-details-marker { display:none; }
    .trace-v2-routing > summary::after { content:'展开明细'; margin-left:auto; color:#98a2b3; font-size:8px; font-weight:400; }
    .trace-v2-routing[open] > summary { border-bottom:1px solid #edf0f3; background:#f8fafc; }
    .trace-v2-routing[open] > summary::after { content:'收起明细'; }
    .trace-v2-routing-title { color:#344054; }
    .trace-v2-routing-level { padding:1px 5px; color:#175cd3; background:#eff8ff; border-radius:3px; font-size:8px; font-weight:600; }
    .trace-v2-routing-level.is-complex { color:#b42318; background:#fff1f0; }
    .trace-v2-routing-level.is-normal { color:#9a5a00; background:#fff7e6; }
    .trace-v2-routing-body { display:grid; gap:5px; padding:7px; }
    .trace-v2-routing-overview { display:flex; flex-wrap:wrap; gap:3px 8px; color:#667085; font-size:9px; line-height:1.4; }
    .trace-v2-routing-signal { color:#475467; }
    .trace-v2-routing-rows { display:grid; gap:3px; }
    .trace-v2-routing-row { min-width:0; padding:5px 6px; background:#fff; border:1px solid #e7edf4; border-left:3px solid #98a2b3; border-radius:4px; }
    .trace-v2-routing-row.is-fallback { border-left-color:#f79009; }
    .trace-v2-routing-row-head { display:flex; align-items:center; gap:4px; min-width:0; }
    .trace-v2-routing-stage { min-width:0; overflow:hidden; color:#344054; font-size:9px; font-weight:700; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-routing-model { min-width:0; margin-left:auto; overflow:hidden; color:#667085; font:8px ui-monospace,SFMono-Regular,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
    .trace-v2-routing-meta { margin-top:3px; color:#667085; font-size:8px; line-height:1.4; word-break:break-word; }
    .trace-v2-routing-badge { flex:0 0 auto; padding:1px 4px; color:#087443; background:#ecfdf3; border-radius:3px; font-size:8px; }
    .trace-v2-routing-badge.is-fallback { color:#9a5a00; background:#fff7e6; }
    .trace-v2-routing-warning { padding:4px 5px; color:#9a5a00; background:#fff7e6; border-radius:4px; font-size:8px; line-height:1.4; }
    .trace-v2-indent-1 { margin-left:8px; }.trace-v2-indent-2 { margin-left:16px; }.trace-v2-indent-3 { margin-left:24px; }
    @media(max-width:620px){.trace-v2-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.trace-v2-indent-1,.trace-v2-indent-2,.trace-v2-indent-3{margin-left:0}.trace-v2-quick{max-width:72px}}
  `;
  document.head.appendChild(style);
  const tracePanelRef = document.querySelector('#trace-panel');

  function ensureTracePanel() {
    const panel = document.querySelector('#trace-panel') || tracePanelRef;
    if (!panel) return null;
    const drawer = document.querySelector('#trace-drawer');
    if (drawer && !drawer.contains(panel)) drawer.appendChild(panel);
    return panel;
  }

  function setTracePanelOpen() {
    const panel = document.querySelector('#trace-panel') || tracePanelRef;
    if (!panel) return;
    panel.classList.remove('is-collapsed');
  }

  function setDrawerOpen(open) {
    const drawer = document.querySelector('#trace-drawer');
    const backdrop = document.querySelector('#trace-drawer-backdrop');
    if (!drawer || !backdrop) return;
    drawer.classList.toggle('is-open', Boolean(open));
    drawer.setAttribute('aria-hidden', String(!open));
    backdrop.classList.toggle('is-visible', Boolean(open));
  }

  function updateTraceAction(summary) {
    const bubble = window.__deepagentActiveTraceBubble;
    const label = bubble && bubble.querySelector('.answer-observability-text');
    if (label) label.textContent = summary || '查看模型、检索、工具和校验详情';
  }

  function liveStageItemsFromTrace(value) {
    const trace = value && !Array.isArray(value) ? value : { steps: Array.isArray(value) ? value : [] };
    const steps = Array.isArray(trace.steps) ? trace.steps : [];
    if (steps.length) {
      return steps.map((step, index) => ({
        name: String(step?.name || step?.node_name || step?.node_id || `执行阶段 ${index + 1}`).replace(/^阶段：\s*/, ''),
        detail: String(step?.detail || '').trim(),
        status: String(step?.status || (index === steps.length - 1 ? 'running' : 'success')),
        duration_ms: step?.duration_ms
      }));
    }
    const spans = Array.isArray(trace.spans) ? trace.spans.filter(item => item && typeof item === 'object') : [];
    if (!spans.length) return [];
    return groupTraceItems(spans).map(group => {
      const primary = group.entries.find(item => item?.type === 'stage') || group.entries[0] || {};
      const status = group.status || primary.status || 'success';
      return {
        name: String(primary.name || traceGroupLabels[group.bucket] || '执行阶段').replace(/^阶段：\s*/, ''),
        detail: String(primary.attributes?.detail || primary.detail || group.details?.[0] || '').trim(),
        status,
        duration_ms: groupDuration(group)
      };
    });
  }

  function liveStageMarkup(item, index, total) {
    const status = String(item?.status || (index === total - 1 ? 'running' : 'success'));
    const stateClass = status === 'error' ? 'is-error' : (status === 'running' ? 'is-current' : 'is-done');
    const stateText = status === 'running' ? '进行中' : statusLabel(status);
    const duration = item?.duration_ms != null && status !== 'running' ? ` · ${formatDuration(item.duration_ms)}` : '';
    return `<div class="answer-live-stage ${stateClass}" title="${esc(item?.detail || '')}"><i class="answer-live-stage-dot"></i><span class="answer-live-stage-name">${esc(item?.name || '执行阶段')}</span><span class="answer-live-stage-state">${esc(stateText + duration)}</span></div>`;
  }

  function renderLiveStageItems(items) {
    const bubble = window.__deepagentActiveTraceBubble;
    if (!bubble || !bubble.isConnected) return;
    let box = bubble.querySelector('.answer-live-trace');
    const visible = (Array.isArray(items) ? items : []).filter(item => item && item.name).slice(-8);
    if (!visible.length) {
      if (box) box.remove();
      return;
    }
    const markup = `<div class="answer-live-trace-head"><span>执行阶段</span><span>实时更新</span></div><div class="answer-live-stage-list">${visible.map((item, index) => liveStageMarkup(item, index, visible.length)).join('')}</div>`;
    if (!box) {
      box = document.createElement('div');
      box.className = 'answer-live-trace';
      const action = bubble.querySelector('.answer-observability');
      if (action) bubble.insertBefore(box, action);
      else bubble.appendChild(box);
    }
    if (box.innerHTML !== markup) box.innerHTML = markup;
  }

  function renderLiveStages(value) {
    const items = window.__deepagentLiveProgressItems?.length
      ? window.__deepagentLiveProgressItems
      : liveStageItemsFromTrace(value);
    renderLiveStageItems(items);
  }

  function updateLiveStage(name, detail, status = 'running') {
    if (!window.__deepagentRunStartedAt) return;
    const label = String(name || '').trim();
    if (!label) return;
    const items = Array.isArray(window.__deepagentLiveProgressItems)
      ? window.__deepagentLiveProgressItems
      : [];
    const index = items.findIndex(item => String(item?.name || '') === label);
    if (index < 0) {
      items.forEach(item => {
        if (String(item?.status || '') === 'running') item.status = 'success';
      });
      items.push({ name: label, detail: String(detail || ''), status });
    } else {
      items[index] = { ...items[index], detail: String(detail || items[index].detail || ''), status };
    }
    window.__deepagentLiveProgressItems = items;
    window.__deepagentLiveTraceValue = items;
    renderLiveStageItems(items);
  }

  function formatElapsed(ms) {
    const totalSeconds = Math.max(0, Math.floor(Number(ms || 0) / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    if (minutes > 0) return `${minutes} 分 ${seconds} 秒`;
    return `${seconds} 秒`;
  }

  function updateRunElapsed(ms) {
    const bubble = window.__deepagentActiveTraceBubble;
    const label = bubble && bubble.querySelector('.answer-observability-duration');
    if (label) label.textContent = `用时 ${formatElapsed(ms)}`;
  }

  function startRunTimer() {
    if (!window.__deepagentRunStartedAt) window.__deepagentRunStartedAt = Date.now();
    const bubble = window.__deepagentActiveTraceBubble;
    if (bubble && bubble.isConnected) {
      ensureTraceAction(bubble);
      if (window.__deepagentRunBubbleObserver) window.__deepagentRunBubbleObserver.disconnect();
      window.__deepagentRunBubbleObserver = new MutationObserver(() => {
        if (window.__deepagentRunStartedAt && bubble.isConnected && !bubble.querySelector('.answer-observability')) {
          ensureTraceAction(bubble);
        }
        if (window.__deepagentRunStartedAt && bubble.isConnected && window.__deepagentLiveProgressItems?.length && !bubble.querySelector('.answer-live-trace')) {
          renderLiveStages(window.__deepagentLiveTraceValue);
        }
      });
      window.__deepagentRunBubbleObserver.observe(bubble, { childList: true });
    }
    updateRunElapsed(Date.now() - window.__deepagentRunStartedAt);
    if (window.__deepagentRunTimer) return;
    window.__deepagentRunTimer = window.setInterval(() => {
      if (window.__deepagentRunStartedAt) {
        updateRunElapsed(Date.now() - window.__deepagentRunStartedAt);
      }
    }, 1000);
  }

  function stopRunTimer(durationMs) {
    if (window.__deepagentRunTimer) {
      window.clearInterval(window.__deepagentRunTimer);
      window.__deepagentRunTimer = null;
    }
    if (window.__deepagentRunBubbleObserver) {
      window.__deepagentRunBubbleObserver.disconnect();
      window.__deepagentRunBubbleObserver = null;
    }
    const provided = Number(durationMs);
    const elapsed = Number.isFinite(provided) && provided >= 0
      ? provided
      : (window.__deepagentRunStartedAt ? Date.now() - window.__deepagentRunStartedAt : (window.__deepagentRunElapsedMs || 0));
    window.__deepagentRunElapsedMs = elapsed;
    window.__deepagentRunStartedAt = null;
    updateRunElapsed(elapsed);
  }

  function ensureTraceAction(bubble, traceValue) {
    if (!bubble || !bubble.isConnected) return;
    let action = bubble.querySelector('.answer-observability');
    if (!action) {
      action = document.createElement('div');
      action.className = 'answer-observability';
      action.innerHTML = '<span class="answer-observability-label">执行摘要</span><span class="answer-observability-text">查看模型、检索、工具和校验详情</span><span class="answer-observability-duration">用时 0 秒</span><button type="button">执行详情</button>';
      bubble.appendChild(action);
      const button = action.querySelector('button');
      button.addEventListener('click', () => {
        window.__deepagentActiveTraceBubble = bubble;
        const trace = button.__traceData || window.__deepagentLastTrace || {};
        renderTrace(trace, false);
        setDrawerOpen(true);
      });
    }
    const button = action.querySelector('button');
    if (button && traceValue) button.__traceData = traceValue;
    updateTraceAction(document.querySelector('#trace-summary')?.textContent || '查看模型、检索、工具和校验详情');
    updateRunElapsed(window.__deepagentRunStartedAt ? Date.now() - window.__deepagentRunStartedAt : (window.__deepagentRunElapsedMs || 0));
    if (window.__deepagentRunStartedAt && window.__deepagentLiveProgressItems?.length) renderLiveStages(window.__deepagentLiveTraceValue);
  }

  document.querySelector('#trace-drawer-close')?.addEventListener('click', () => setDrawerOpen(false));
  document.querySelector('#trace-drawer-backdrop')?.addEventListener('click', () => setDrawerOpen(false));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') setDrawerOpen(false); });
  window.openTraceDrawer = () => setDrawerOpen(true);

  function formatNumber(value) {
    if (value == null || value === '') return '—';
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString('zh-CN') : esc(value);
  }

  function formatDuration(value) {
    if (value == null || value === '') return '—';
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    if (number < 1000) return `${Math.max(0, Math.round(number))} ms`;
    return `${(number / 1000).toFixed(number >= 10000 ? 1 : 2)} s`;
  }

  const statusLabels = {
    success: '成功', error: '失败', running: '运行中', queued: '排队中', pending: '待处理',
    waiting_confirmation: '等待确认', awaiting_confirmation: '等待确认', awaiting_approval: '等待审批',
    skipped: '已跳过', partial: '部分完成', complete: '已完成', unknown: '未知',
    expired: '已过期', rejected: '已拒绝', canceled: '已取消', cancelled: '已取消', timeout: '已超时',
    supported: '证据充分', unsupported: '未支持', conflict: '证据冲突',
    not_found: '未找到可靠证据', retrieval_error: '知识库调用失败', not_retrieved: '应检索但未检索',
    answer_blocked: '答案被安全校验拦截', unverified: '未经过后端证据校验',
    not_required: '无需知识证据', business_action: '业务工具结果', agent_error: 'Agent 执行失败',
    missing: '缺失', legacy: '旧格式'
  };
  const evidenceStatusLabels = {
    supported: '证据充分', unverified: '未经过后端证据校验', partial: '部分证据',
    conflict: '证据冲突', not_found: '未找到可靠证据', retrieval_error: '知识库调用失败',
    not_retrieved: '应检索但未检索', answer_blocked: '答案被安全校验拦截',
    agent_error: 'Agent 执行失败', not_required: '无需知识证据', business_action: '业务工具结果',
    unsupported: '未支持'
  };

  function statusLabel(status) {
    const key = String(status == null ? '' : status).trim();
    return statusLabels[key] || status || '完成';
  }

  function evidenceStatusLabel(status) {
    const key = String(status == null ? '' : status).trim();
    return evidenceStatusLabels[key] || statusLabel(key) || '未知';
  }

  // 供工作台中的旧版内联渲染函数复用，内部状态码仍保持英文。
  window.deepAgentStatusLabel = statusLabel;
  window.deepAgentEvidenceStatusLabel = evidenceStatusLabel;

  function tokenSourceLabel(source) {
    return ({ provider: '供应商返回', partial: '部分返回', estimated: '估算', unavailable: '未返回' }[source]) || source || '未返回';
  }

  function normalizeTrace(value, running) {
    if (!running && value && !Array.isArray(value)) return value || {};
    if (running && value && !Array.isArray(value)) return value || {};
    if (running) return { trace_id: null, spans: [], steps: Array.isArray(value) ? value : [] };
    return window.__deepagentLastTrace || { spans: [], steps: Array.isArray(value) ? value : [] };
  }

  function spanDepth(span, byId) {
    let depth = 0;
    let parent = span && span.parent_span_id;
    const seen = new Set();
    while (parent && byId[parent] && depth < 3 && !seen.has(parent)) {
      seen.add(parent);
      depth += 1;
      parent = byId[parent].parent_span_id;
    }
    return depth;
  }

  function buildStageTiming(spans) {
    const timings = new Map();
    let currentStageId = null;
    spans.forEach(span => {
      if (span.type === 'stage') {
        currentStageId = span.span_id || null;
        if (currentStageId) timings.set(currentStageId, { modelMs: 0 });
        return;
      }
      if (!currentStageId || span.type !== 'llm') return;
      const duration = Number(span.duration_ms);
      if (!Number.isFinite(duration)) return;
      const timing = timings.get(currentStageId);
      if (timing) timing.modelMs += Math.max(0, duration);
    });
    return timings;
  }

  function spanDetail(span, stageTimings) {
    const attrs = span.attributes || {};
    const details = [];
    if (span.type === 'llm') {
      if (span.model) details.push(`模型 ${span.model}`);
      if (span.provider && span.provider !== 'qwen') details.push(`提供方 ${span.provider}`);
      if (span.model_calls > 1) details.push(`${formatNumber(span.model_calls)} 次模型请求`);
      if (attrs.input_chars != null) details.push(`输入 ${formatNumber(attrs.input_chars)} 字符`);
    } else if (span.type === 'tool') {
      if (span.tool_name) details.push(`工具 ${span.tool_name}`);
      if (span.connection_id) details.push(`连接 ${span.connection_id}`);
    } else if (span.type === 'retriever' || span.type === 'api') {
      const source = span.component || attrs.source_type;
      if (source) details.push(componentLabels[source] || source);
      if (attrs.result_count != null) details.push(`返回 ${formatNumber(attrs.result_count)} 条`);
      if (span.tool_name) details.push(`入口 ${span.tool_name}`);
      if (span.connection_id) details.push(`连接 ${span.connection_id}`);
    } else if (attrs.node_type) {
      details.push(`节点类型 ${attrs.node_type}`);
    }
    if (attrs.detail) details.push(String(attrs.detail));
    if (span.type === 'stage' && span.span_id && stageTimings) {
      const timing = stageTimings.get(span.span_id);
      const stageDuration = Number(span.duration_ms);
      if (timing && timing.modelMs > 0 && Number.isFinite(stageDuration)) {
        const orchestrationMs = Math.max(0, stageDuration - timing.modelMs);
        details.push(`模型调用耗时合计 ${formatDuration(timing.modelMs)} · 编排与解析 ${formatDuration(orchestrationMs)}`);
      }
    }
    if (span.input_tokens != null || span.output_tokens != null) {
      details.push(`Token 输入 ${formatNumber(span.input_tokens)} / 输出 ${formatNumber(span.output_tokens)} / 合计 ${formatNumber(span.total_tokens)}`);
    }
    return details.join(' · ');
  }

  function spanMarkup(span, index, byId, stageTimings) {
    const type = span.type || 'stage';
    const status = span.status || 'success';
    const component = span.component ? (componentLabels[span.component] || span.component) : '';
    const depth = spanDepth(span, byId);
    const detail = spanDetail(span, stageTimings);
    const tokens = type === 'llm' && (span.input_tokens != null || span.output_tokens != null)
      ? `<span class="trace-v2-chip">Token ${formatNumber(span.total_tokens)}</span>` : '';
    const quick = type === 'llm' ? span.model : (type === 'tool' || type === 'retriever' || type === 'api' ? (span.tool_name || component) : component);
    return `<div class="trace-v2-row ${esc(type)} ${status === 'error' ? 'error' : ''} ${status === 'running' ? 'running' : ''} trace-v2-indent-${depth}" title="${esc(span.span_id || '')}">
      <div class="trace-v2-row-head"><span class="trace-v2-kind">${esc(typeLabels[type] || type.toUpperCase())}</span><strong>${esc(span.name || `执行阶段 ${index + 1}`)}</strong>${quick ? `<span class="trace-v2-quick">${esc(quick)}</span>` : ''}<span class="trace-v2-chip status-${esc(status)}">${esc(statusLabel(status))}</span>${tokens}<span class="trace-v2-duration">${esc(formatDuration(span.duration_ms))}</span></div>
      ${detail ? `<div class="trace-v2-detail">${esc(detail)}</div>` : ''}
      ${span.error ? `<div class="trace-v2-error">${esc(span.error)}</div>` : ''}
    </div>`;
  }

  const traceGroupLabels = {
    understanding: '请求理解',
    planning: '需求规划',
    retrieval: '知识检索',
    evidence: '证据校验',
    answer: '回答生成',
    other: '其他执行'
  };

  function phaseBucketForSpan(span, currentBucket) {
    const name = String(span?.name || '').toLowerCase();
    const component = String(span?.component || '').toLowerCase();
    const type = String(span?.type || '').toLowerCase();
    const text = `${name} ${component}`;
    if (text.includes('意图') || text.includes('实体') || text.includes('router') || text.includes('路由')) return 'understanding';
    if (text.includes('证据校验') || text.includes('证据裁判') || text.includes('claim_judge') || text.includes('需求结果生成') || text.includes('逐项证据裁判')) return 'evidence';
    if (text.includes('需求规划') || text.includes('requirement_plan') || text.includes('覆盖审计')) return 'planning';
    if (text.includes('统一搜索') || text.includes('unified_search') || type === 'retriever' || type === 'api') return 'retrieval';
    if (text.includes('引用回答') || text.includes('最终回答') || text.includes('answer')) return 'answer';
    return currentBucket || (type === 'llm' ? 'answer' : 'other');
  }

  function phaseBucketForStep(step, currentBucket) {
    return phaseBucketForSpan({
      name: step?.name || '',
      component: 'workflow',
      type: 'stage'
    }, currentBucket);
  }

  function groupTraceItems(items) {
    const groups = [];
    const byBucket = new Map();
    let currentBucket = null;
    (items || []).forEach(item => {
      const bucket = phaseBucketForSpan(item, currentBucket);
      currentBucket = bucket;
      let group = byBucket.get(bucket);
      if (!group) {
        group = { bucket, entries: [], details: [], stageDurations: [], status: 'success' };
        byBucket.set(bucket, group);
        groups.push(group);
      }
      group.entries.push(item);
      const detail = item?.attributes?.detail || item?.detail;
      if (detail && !group.details.includes(String(detail))) group.details.push(String(detail));
      if (item?.type === 'stage' && Number.isFinite(Number(item.duration_ms))) {
        group.stageDurations.push(Math.max(0, Number(item.duration_ms)));
      }
      if (item?.status === 'error') group.status = 'error';
      else if (item?.status === 'running' && group.status !== 'error') group.status = 'running';
    });
    return groups;
  }

  function groupDuration(group) {
    if (group.stageDurations.length) return group.stageDurations.reduce((sum, value) => sum + value, 0);
    return group.entries.reduce((sum, item) => {
      const value = Number(item?.duration_ms);
      return sum + (Number.isFinite(value) ? Math.max(0, value) : 0);
    }, 0);
  }

  function isUnifiedSearchOrchestrator(span) {
    const name = String(span?.name || '').toLowerCase();
    const component = String(span?.component || '').toLowerCase();
    const toolName = String(span?.tool_name || '').toLowerCase();
    return component === 'unified_search'
      || toolName === 'unified_search'
      || name.includes('统一搜索编排器');
  }

  function retrievalSourceEntries(group) {
    return group.entries.filter(item =>
      ['retriever', 'api'].includes(item?.type) && !isUnifiedSearchOrchestrator(item)
    );
  }

  function retrievalResultCount(group) {
    const orchestrator = group.entries.find(isUnifiedSearchOrchestrator);
    const value = orchestrator?.attributes?.result_count ?? orchestrator?.result_count;
    const count = Number(value);
    return Number.isFinite(count) ? Math.max(0, count) : null;
  }

  function retrievalRawCount(sourceEntries) {
    const counts = sourceEntries
      .map(item => Number(item?.attributes?.result_count ?? item?.result_count))
      .filter(Number.isFinite);
    return counts.length ? counts.reduce((sum, value) => sum + Math.max(0, value), 0) : null;
  }

  function retrievalSummaryMarkup(sourceEntries, finalCount) {
    const rawCount = retrievalRawCount(sourceEntries);
    const sourceNames = sourceEntries
      .map(item => componentLabels[item?.component] || item?.source_name || item?.name)
      .filter(Boolean);
    return `<div class="trace-v2-retrieval-summary">
      <div><span>数据源</span><b>${esc(formatNumber(sourceEntries.length))} 个</b></div>
      <div><span>各源返回</span><b>${rawCount == null ? '—' : `${esc(formatNumber(rawCount))} 条`}</b></div>
      <div><span>融合结果</span><b>${finalCount == null ? '—' : `${esc(formatNumber(finalCount))} 条`}</b></div>
      ${sourceNames.length ? `<div style="grid-column:1/-1"><span>来源</span><b>${esc([...new Set(sourceNames)].join('、'))}</b></div>` : ''}
    </div>`;
  }

  function groupMarkup(group, index, byId, stageTimings, running, promoteSingleStage = true) {
    const modelCalls = group.entries
      .filter(item => item?.type === 'llm')
      .reduce((sum, item) => sum + Math.max(0, Number(item.model_calls || 0)), 0);
    const unifiedRetrieval = group.bucket === 'retrieval'
      && group.entries.some(isUnifiedSearchOrchestrator);
    const sourceEntries = unifiedRetrieval ? retrievalSourceEntries(group) : [];
    const finalRetrievalCount = unifiedRetrieval ? retrievalResultCount(group) : null;
    const toolCalls = group.entries
      .filter(item => ['tool', 'retriever', 'api'].includes(item?.type)).length;
    const duration = groupDuration(group);
    const status = group.status || 'success';
    const details = group.details.slice(0, 4).join('；');
    const stageEntries = group.entries.filter(item => item?.type === 'stage');
    const primaryStage = promoteSingleStage && stageEntries.length === 1 ? stageEntries[0] : null;
    const primaryTitle = primaryStage
      ? String(primaryStage.name || '').replace(/^阶段：\s*/, '')
      : (traceGroupLabels[group.bucket] || group.bucket);
    const primaryDetail = primaryStage
      ? (unifiedRetrieval
        ? `多数据源检索完成 · ${sourceEntries.length} 个数据源 · ${finalRetrievalCount == null ? '暂无' : `${formatNumber(finalRetrievalCount)} 条融合结果`} · 编排入口 unified_search`
        : (spanDetail(primaryStage, stageTimings) || details))
      : '';
    const visibleDuration = primaryStage ? primaryStage.duration_ms : duration;
    const detailCount = primaryStage ? Math.max(0, group.entries.length - 1) : group.entries.length;
    const stats = unifiedRetrieval
      ? [
        `${formatNumber(sourceEntries.length)} 个数据源`,
        finalRetrievalCount == null ? '' : `${formatNumber(finalRetrievalCount)} 条融合结果`
      ].filter(Boolean).join(' · ')
      : [
        `${detailCount} 个明细`,
        modelCalls ? `${formatNumber(modelCalls)} 次模型` : '',
        toolCalls ? `${formatNumber(toolCalls)} 个工具/数据` : ''
      ].filter(Boolean).join(' · ');
    const bodyEntries = primaryStage
      ? group.entries.filter(item => item !== primaryStage && !(unifiedRetrieval && isUnifiedSearchOrchestrator(item)))
      : group.entries.filter(item => !(unifiedRetrieval && isUnifiedSearchOrchestrator(item)));
    const retrievalSummary = unifiedRetrieval ? retrievalSummaryMarkup(sourceEntries, finalRetrievalCount) : '';
    const summaryDetail = primaryStage ? primaryDetail : details;
    const summary = `<div class="trace-v2-group-summary-head"><span class="trace-v2-kind">阶段</span><span class="trace-v2-group-title">${esc(primaryTitle || '执行阶段')}</span><span class="trace-v2-chip status-${esc(status)}">${esc(statusLabel(status))}</span><span class="trace-v2-group-meta"><span class="trace-v2-quick">${esc(stats)}</span><span class="trace-v2-duration">${esc(formatDuration(primaryStage ? visibleDuration : duration))}</span></span></div>${summaryDetail ? `<div class="trace-v2-group-summary-detail">${esc(summaryDetail)}</div>` : ''}`;
    const groupClass = `trace-v2-group ${primaryStage ? `trace-v2-group-single ${esc(group.bucket)}` : ''}`;
    if (group.bucket === 'answer') {
      return `<div class="${groupClass} trace-v2-group-static trace-v2-group-static-answer"><div class="trace-v2-group-static-head">${summary}</div></div>`;
    }
    return `<details class="trace-v2-group ${primaryStage ? `trace-v2-group-single ${esc(group.bucket)}` : ''}">
      <summary>${summary}</summary>
       <div class="trace-v2-group-body">${retrievalSummary}${bodyEntries.map((span, childIndex) => spanMarkup(span, childIndex, byId, stageTimings)).join('')}</div>
     </details>`;
  }

  function modelRoutingMarkup(trace) {
    const routing = trace && typeof trace === 'object'
      ? trace.routing_policy?.model_routing
      : null;
    if (!routing || typeof routing !== 'object') return '';

    const complexity = routing.complexity && typeof routing.complexity === 'object'
      ? routing.complexity
      : {};
    const level = String(complexity.level || 'normal');
    const levelLabel = ({ simple: '简单', normal: '常规', complex: '复杂' }[level]) || level;
    const score = complexity.score == null ? '—' : formatNumber(complexity.score);
    const signals = Array.isArray(complexity.signals) && complexity.signals.length
      ? complexity.signals.join('、')
      : '无显式复杂度信号';
    const stages = routing.stages && typeof routing.stages === 'object'
      ? Object.entries(routing.stages)
      : [];
    const rows = stages.map(([stage, item]) => {
      const value = item && typeof item === 'object' ? item : {};
      const requested = String(value.requested_profile_id || '—');
      const selected = String(value.profile_id || '—');
      const model = String(value.model || '未记录');
      const fallback = Boolean(value.fallback_used) || (requested !== '—' && selected !== requested);
      const stageName = String(value.stage_name || stage);
      const fallbackReason = fallback
        ? String(value.reason || '后端未返回具体回退原因')
        : '未发生回退';
      const profileText = `<span>请求档案：${esc(requested)}</span><span>实际档案：${esc(selected)}</span><span>是否回退：${fallback ? '是' : '否'}</span><span>回退原因：${esc(fallbackReason)}</span>`;
      const provider = value.provider ? `<span>提供方：${esc(String(value.provider))}</span>` : '';
      return `<div class="trace-v2-routing-row ${fallback ? 'is-fallback' : ''}">
        <div class="trace-v2-routing-row-head"><span class="trace-v2-routing-stage">${esc(stageName)}</span><span class="trace-v2-routing-model" title="${esc(model)}">${esc(model)}</span><span class="trace-v2-routing-badge ${fallback ? 'is-fallback' : ''}">${fallback ? '已回退' : '已选择'}</span></div>
        <div class="trace-v2-routing-meta">${profileText}${provider}</div>
      </div>`;
    }).join('');

    const disabled = routing.disabled === true;
    const configuredStageCount = stages.length;
    const actualModelCalls = trace?.model_calls == null ? '—' : `${formatNumber(trace.model_calls)} 次实际调用`;
    const title = disabled ? '未启用' : `复杂度：${levelLabel} · ${configuredStageCount} 个配置阶段`;
    const titleClass = `trace-v2-routing-level ${level === 'complex' ? 'is-complex' : level === 'normal' ? 'is-normal' : ''}`;
    const warning = routing.warning ? `<div class="trace-v2-routing-warning">${esc(String(routing.warning))}</div>` : '';
    const overview = disabled
      ? '<div class="trace-v2-routing-overview"><span>当前请求未启用阶段模型路由，使用兼容模式。</span></div>'
      : `<div class="trace-v2-routing-overview"><span>路由配置：${formatNumber(configuredStageCount)} 个阶段</span><span>实际模型调用：${esc(actualModelCalls)}</span><span>评分：${esc(score)}</span><span class="trace-v2-routing-signal">命中：${esc(signals)}</span></div>`;
    const stageBody = rows.length
      ? `<div class="trace-v2-routing-rows">${rows}</div>`
      : '<div class="trace-v2-routing-overview">没有可展示的阶段路由记录。</div>';
    return `<details class="trace-v2-routing">
      <summary><span class="trace-v2-routing-title">模型阶段路由</span><span class="${titleClass}">${esc(title)}</span></summary>
      <div class="trace-v2-routing-body">${overview}${warning}${stageBody}</div>
    </details>`;
  }

  function answerAssemblyMarkup(trace) {
    const assembly = trace && typeof trace === 'object'
      ? trace.routing_policy?.answer_assembly
      : null;
    if (!assembly || typeof assembly !== 'object') return '';
    const gate = String(assembly.gate || 'not_run');
    const gateLabels = {
      passed: '直接通过',
      passed_after_repair: '修复后通过',
      fact_plan_rendered: '事实计划已渲染',
      fact_plan_repaired: '事实计划修复后通过',
      repair_required: '需要修复',
      soft_pass_bound_to_judged_results: '软通过（已绑定裁判证据）',
      safe_result_fallback: '安全结果回退',
      fact_plan_fallback: '事实计划安全回退',
      not_run: '未执行'
    };
    const mode = String(assembly.mode || 'fact_binding');
    const bindingMode = String(assembly.binding_mode || 'fact_plan');
    const attempted = assembly.repair_attempted === true ? '是' : '否';
    const succeeded = assembly.repair_succeeded === true ? '是' : '否';
    const factCount = assembly.fact_count == null ? '—' : formatNumber(assembly.fact_count);
    const boundFactCount = assembly.bound_fact_count == null ? '—' : formatNumber(assembly.bound_fact_count);
    const renderedFactCount = assembly.rendered_fact_count == null ? '—' : formatNumber(assembly.rendered_fact_count);
    const invalidFactRefs = Array.isArray(assembly.invalid_fact_refs) ? assembly.invalid_fact_refs.length : 0;
    const droppedSegments = assembly.dropped_segments == null ? 0 : Number(assembly.dropped_segments) || 0;
    const violations = Array.isArray(assembly.violations) ? assembly.violations : [];
    const warning = violations.length
      ? `<div class="trace-v2-routing-warning">${esc(violations.join('；'))}</div>`
      : '';
    return `<details class="trace-v2-routing">
      <summary><span class="trace-v2-routing-title">答案组装与证据绑定</span><span class="trace-v2-routing-level">${esc(gateLabels[gate] || gate)}</span></summary>
      <div class="trace-v2-routing-body">
         <div class="trace-v2-routing-overview"><span>组装模式：${esc(mode)}</span><span>绑定契约：${esc(bindingMode)}</span><span>事实账本：${factCount}</span><span>已绑定：${boundFactCount}</span><span>已渲染：${renderedFactCount}</span><span>修复尝试：${attempted}</span><span>修复成功：${succeeded}</span></div>
         <div class="trace-v2-routing-overview"><span>无效事实引用：${invalidFactRefs}</span><span>丢弃片段：${formatNumber(droppedSegments)}</span></div>
        ${warning || '<div class="trace-v2-routing-overview">事实值均由服务端事实账本渲染，未执行自由文本逐字比对。</div>'}
      </div>
    </details>`;
  }

  function groupedStepMarkup(steps, running) {
    const groups = [];
    const byBucket = new Map();
    let currentBucket = null;
    (steps || []).forEach(step => {
      const bucket = phaseBucketForStep(step, currentBucket);
      currentBucket = bucket;
      let group = byBucket.get(bucket);
      if (!group) {
        group = { bucket, entries: [], details: [], stageDurations: [], status: 'success' };
        byBucket.set(bucket, group);
        groups.push(group);
      }
      group.entries.push({
        span_id: `legacy-step-${groups.length}-${group.entries.length}`,
        type: 'stage',
        name: `阶段：${step.name || '执行步骤'}`,
        status: step.status || 'success',
        duration_ms: step.duration_ms,
        attributes: { detail: step.detail || '' }
      });
      if (step.detail && !group.details.includes(String(step.detail))) group.details.push(String(step.detail));
      if (Number.isFinite(Number(step.duration_ms))) group.stageDurations.push(Math.max(0, Number(step.duration_ms)));
      if (step.status === 'error') group.status = 'error';
      else if (step.status === 'running' && group.status !== 'error') group.status = 'running';
    });
    const byId = Object.fromEntries(groups.flatMap(group => group.entries).map(item => [item.span_id, item]));
    return groups.map((group, index) => groupMarkup(group, index, byId, new Map(), running, false)).join('');
  }

  function renderTrace(value, running) {
    ensureTracePanel(window.__deepagentActiveTraceBubble);
    if (running) {
      const spans = value && !Array.isArray(value) && Array.isArray(value.spans) ? value.spans : [];
      if (!window.__deepagentLiveProgressItems?.length || (window.__deepagentLiveProgressItems.length === 1 && window.__deepagentLiveProgressItems[0].name === '启动任务' && spans.length)) {
        window.__deepagentLiveProgressItems = liveStageItemsFromTrace(value);
      }
      window.__deepagentLiveTraceValue = value;
      renderLiveStages(value);
    }
    const box = document.querySelector('#trace');
    if (!box) return;
    const trace = normalizeTrace(value, running);
    const spans = Array.isArray(trace.spans) ? trace.spans.filter(item => item && typeof item === 'object') : [];
    const stageTimings = buildStageTiming(spans);
    const steps = Array.isArray(trace.steps) ? trace.steps : [];
    const traceSummary = document.querySelector('#trace-summary');
    ensureTraceAction(window.__deepagentActiveTraceBubble, trace);
    if (running) setTracePanelOpen(true);
    box.className = 'trace trace-v2';
    if (!spans.length) {
      if (!steps.length) {
        if (traceSummary) traceSummary.textContent = running ? '执行中…' : '等待执行';
        updateTraceAction(traceSummary?.textContent);
        box.innerHTML = '<div class="trace-empty">暂无执行步骤</div>';
        return;
      }
      const groupedSteps = groupedStepMarkup(steps, running);
       if (traceSummary) traceSummary.textContent = `${formatNumber(new Set(steps.map(item => phaseBucketForStep(item, null))).size)} 个执行主阶段 · ${formatNumber(steps.length)} 条执行明细`;
      updateTraceAction(traceSummary?.textContent);
       box.innerHTML = `${modelRoutingMarkup(trace)}${answerAssemblyMarkup(trace)}<div class="trace-v2-groups">${groupedSteps}</div>`;
      return;
    }
    const usage = trace.token_usage || {};
    const llmSpans = spans.filter(item => item.type === 'llm');
    const spanHasTokenUsage = llmSpans.some(item => item.input_tokens != null || item.output_tokens != null || item.total_tokens != null);
    const spanModelCalls = llmSpans.reduce((sum, item) => sum + Number(item.model_calls || 0), 0);
    const spanInputTokens = llmSpans.reduce((sum, item) => sum + (item.input_tokens == null ? 0 : Number(item.input_tokens) || 0), 0);
    const spanOutputTokens = llmSpans.reduce((sum, item) => sum + (item.output_tokens == null ? 0 : Number(item.output_tokens) || 0), 0);
    const spanTotalTokens = llmSpans.reduce((sum, item) => {
      if (item.input_tokens != null && item.output_tokens != null) return sum + (Number(item.input_tokens) || 0) + (Number(item.output_tokens) || 0);
      return sum + (item.total_tokens == null ? 0 : Number(item.total_tokens) || 0);
    }, 0);
    const modelCalls = llmSpans.length ? spanModelCalls : (trace.model_calls != null ? trace.model_calls : 0);
    const toolCalls = spans.some(item => ['tool', 'retriever', 'api'].includes(item.type))
      ? spans.filter(item => ['tool', 'retriever', 'api'].includes(item.type)).length
      : (trace.tool_span_count != null ? trace.tool_span_count : 0);
    const inputTokens = spanHasTokenUsage ? spanInputTokens : (trace.input_tokens != null ? trace.input_tokens : usage.input_tokens);
    const outputTokens = spanHasTokenUsage ? spanOutputTokens : (trace.output_tokens != null ? trace.output_tokens : usage.output_tokens);
    const totalTokens = spanHasTokenUsage ? spanTotalTokens : (trace.total_tokens != null ? trace.total_tokens : usage.total_tokens);
    const source = trace.token_source || usage.source || 'unavailable';
    const byId = Object.fromEntries(spans.map(item => [item.span_id, item]));
    const groups = groupTraceItems(spans);
    const traceId = trace.trace_id || trace.workflow_run_id || '';
    if (traceSummary) traceSummary.textContent = `${formatNumber(groups.length)} 个执行主阶段 · ${formatNumber(spans.length)} 条执行明细 · ${formatDuration(trace.total_duration_ms)} · ${formatNumber(modelCalls)} 次模型调用 · ${formatNumber(toolCalls)} 次工具/数据`;
    updateTraceAction(traceSummary?.textContent);
    box.innerHTML = `<div class="trace-v2-head"><span class="trace-v2-note" style="margin-top:0">${running ? '实时执行链路' : `${formatNumber(groups.length)} 个执行主阶段 · ${formatNumber(spans.length)} 条执行明细`}</span>${traceId ? `<span class="trace-v2-id" title="${esc(traceId)}">Trace ${esc(traceId)}</span>` : ''}</div>
       <div class="trace-v2-summary"><div class="trace-v2-stat"><span>总耗时</span><b>${esc(formatDuration(trace.total_duration_ms))}</b></div><div class="trace-v2-stat"><span>模型调用</span><b>${esc(formatNumber(modelCalls))}</b></div><div class="trace-v2-stat"><span>工具/数据</span><b>${esc(formatNumber(toolCalls))}</b></div><div class="trace-v2-stat"><span>输入 Token</span><b>${esc(formatNumber(inputTokens))}</b></div><div class="trace-v2-stat"><span>输出 Token</span><b>${esc(formatNumber(outputTokens))}</b></div><div class="trace-v2-stat"><span>Token 合计</span><b>${esc(formatNumber(totalTokens))}</b></div></div>
        ${modelRoutingMarkup(trace)}
        ${answerAssemblyMarkup(trace)}
   <div class="trace-v2-groups">${groups.map((group, index) => groupMarkup(group, index, byId, stageTimings, running)).join('')}</div>
       <div class="trace-v2-note">Token 状态：${esc(tokenSourceLabel(source))}。模型内部推理内容不展示，仅展示调用元数据、工具结果摘要和安全错误。</div>`;
  }

  const legacyRenderResult = window.renderResult;
  const legacyAddMessage = window.addMessage;
  window.__deepagentLastTrace = null;
  window.__deepagentLiveSpans = {};
  window.__deepagentLiveSteps = [];
  window.__deepagentActiveTraceBubble = null;
  if (typeof legacyAddMessage === 'function') {
    window.addMessage = function (text, role, extra) {
      const bubble = legacyAddMessage.call(this, text, role, extra);
      if (role === 'assistant') {
        window.__deepagentActiveTraceBubble = bubble;
        ensureTracePanel(bubble);
      }
      return bubble;
    };
  }
  window.setTrace = function (value, running) {
    ensureTracePanel(window.__deepagentActiveTraceBubble);
    if (running) startRunTimer();
    if (running) {
      window.__deepagentLiveTraceValue = value;
      window.__deepagentLiveProgressItems = liveStageItemsFromTrace(value);
    } else {
      window.__deepagentLiveTraceValue = null;
      window.__deepagentLiveProgressItems = [];
      renderLiveStageItems([]);
    }
    if (!running && Array.isArray(value) && value.length === 0) {
      window.__deepagentLastTrace = null;
      window.__deepagentLiveSpans = {};
      window.__deepagentLiveSteps = [];
      setTracePanelOpen(false);
      setDrawerOpen(false);
    }
    if (running && Array.isArray(value) && value.some(item => String(item && item.name || '').includes('启动任务'))) {
      window.__deepagentLastTrace = null;
      window.__deepagentLiveSpans = {};
    }
    if (running && Array.isArray(value)) window.__deepagentLiveSteps = value;
    renderTrace(value, Boolean(running));
    if (running) renderLiveStages(value);
    if (!running) stopRunTimer(window.__deepagentLastTrace?.total_duration_ms);
  };
  window.renderResult = function (result, bubble) {
    window.__deepagentLiveTraceValue = null;
    window.__deepagentLiveProgressItems = [];
    renderLiveStageItems([]);
    window.__deepagentLastTrace = result && result.trace ? result.trace : null;
    window.__deepagentLiveSpans = {};
    const output = typeof legacyRenderResult === 'function' ? legacyRenderResult(result, bubble) : undefined;
    const evidenceStatus = result && result.evidence && result.evidence.status;
    if (evidenceStatus) {
      const evidenceLabel = evidenceStatusLabel(evidenceStatus);
      const sourceCount = result.citations?.length || result.retrieval_hits?.length || 0;
      const answerMeta = bubble && bubble.querySelector ? bubble.querySelector('.answer-meta') : null;
      if (answerMeta) answerMeta.textContent = `状态：${evidenceLabel} · 来源 ${sourceCount} 个`;
      const metricResult = document.querySelector('#metric-result');
      const evidenceLabelElement = document.querySelector('#evidence-label');
      if (metricResult) metricResult.textContent = evidenceLabel;
      if (evidenceLabelElement) evidenceLabelElement.textContent = evidenceLabel;
    }
    window.__deepagentActiveTraceBubble = bubble || window.__deepagentActiveTraceBubble;
    ensureTracePanel(window.__deepagentActiveTraceBubble);
    renderTrace(window.__deepagentLastTrace || {}, false);
    setTracePanelOpen(false);
    const scroller = bubble && bubble.closest ? bubble.closest('.messages') : null;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
    if (result?.trace?.total_duration_ms != null) stopRunTimer(result.trace.total_duration_ms);
    return output;
  };
  const legacyParseSse = window.parseSse;
  if (typeof legacyParseSse === 'function') {
    window.parseSse = function (block, handler) {
      return legacyParseSse(block, function (event, data) {
        if (event === 'trace_span' && data && data.span) {
          const span = data.span;
          if (span.span_id) {
            window.__deepagentLiveSpans[span.span_id] = span;
            renderTrace({ trace_id: span.trace_id, spans: Object.values(window.__deepagentLiveSpans), steps: window.__deepagentLiveSteps }, true);
          }
        }
        if (event === 'run_started') updateLiveStage('请求理解', '正在解析用户问题、上下文和工作流入口');
        if (event === 'requirement_planning') updateLiveStage('需求规划', '正在拆解用户问题并生成检索需求');
        if (event === 'workflow_started') updateLiveStage('工作流执行', `工作流 ${data?.workflow_id || ''} 已启动`);
        if (event === 'workflow_node') updateLiveStage(data?.node_name || data?.node_id || '工作流节点', data?.detail || data?.status || '', data?.status === 'waiting_confirmation' ? 'running' : (data?.status || 'running'));
        if (event === 'retrieval_started') updateLiveStage('统一搜索', '正在查询已启用的知识库、图谱和业务数据');
        if (event === 'retrieval_completed') updateLiveStage('证据校验', '检索完成，正在筛选和绑定有效证据');
        if (event === 'claim_processing') updateLiveStage('证据裁判与回答生成', '正在逐项校验证据并组织最终回答');
        if (event === 'agent_started') updateLiveStage('回答生成', '大模型正在组织最终回答');
        if (event === 'node_update' && Array.isArray(data?.nodes) && data.nodes.length) updateLiveStage('执行进度', data.nodes.join('、'));
        if (typeof handler === 'function') handler(event, data);
      });
    };
  }
  window.renderTrace = renderTrace;
})();

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { chromium } = require('playwright');
const staticRoot = path.join(__dirname, '../../app/static');

test('shared pagination keeps filtering, selection and independent lists intact', { timeout: 60000 }, async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const rows = Array.from({ length: 12 }, (_, index) => '<tr data-id="' + index + '"><td><input type="checkbox" checked>记录' + index + '</td></tr>').join('');
    await page.setContent('<div class="nav"><button data-view="knowledge" class="active"></button></div><div id="subview">' +
      '<div id="first"><div class="table-scroll"><table class="data-table"><tbody>' + rows + '</tbody></table></div></div>' +
      '<div id="second"><div class="rp-list">' + Array.from({ length: 6 }, (_, i) => '<button data-rp-select="' + i + '">策略' + i + '</button>').join('') + '</div></div>' +
      '<div id="binding"><div class="app-check-grid">' + Array.from({ length: 6 }, (_, i) => '<label class="app-check">来源' + i + '</label>').join('') + '</div></div>' +
      '<table class="answer-table"><tbody>' + rows + '</tbody></table></div>');
    await page.addStyleTag({ path: path.join(staticRoot, 'list-pagination.css') });
    await page.addScriptTag({ path: path.join(staticRoot, 'list-pagination.js') });
    assert.equal(await page.locator('#first tr:visible').count(), 5);
    assert.equal(await page.locator('#second .rp-list button:visible').count(), 5);
    assert.equal(await page.locator('.answer-table tr:visible').count(), 12);
    await page.locator('#first [data-list-page="next"]').click();
    assert.equal(await page.locator('#first tr:visible').first().getAttribute('data-id'), '5');
    assert.equal(await page.locator('#second .rp-list button:visible').first().getAttribute('data-rp-select'), '0');
    assert.equal(await page.locator('#first input:checked').count(), 12);
    await page.locator('#first [data-list-page="next"]').click();
    assert.equal(await page.locator('#first tr:visible').count(), 2);
    assert.equal(await page.locator('#first [data-list-page="next"]').isDisabled(), true);
    await page.evaluate(() => {
      document.querySelectorAll('#first tr').forEach((row, index) => { row.style.display = index % 2 ? 'none' : ''; });
      ListPagination.refresh();
    });
    assert.equal(await page.locator('#first tr:visible').count(), 5);
    assert.match(await page.locator('#first .list-pagination').textContent(), /第 1 \/ 2 页.*共 6 条/);
    await page.locator('#first [data-list-page="next"]').click();
    assert.equal(await page.locator('#first tr:visible').first().getAttribute('data-id'), '10');
    await page.evaluate(() => {
      document.querySelector('#first tbody').innerHTML = '<tr data-id="new"><td>新的筛选结果</td></tr>';
      ListPagination.refresh();
    });
    assert.equal(await page.locator('#first tr:visible').count(), 1);
    assert.equal(await page.locator('#first .list-pagination:visible').count(), 0);
    assert.equal(await page.locator('#second .list-pagination').count(), 1);
    await page.evaluate(() => {
      document.querySelectorAll('#binding .app-check').forEach(label => {
        const wrapper = document.createElement('div');
        wrapper.className = 'app-source-card';
        label.before(wrapper);wrapper.appendChild(label);
      });
      ListPagination.refresh();
    });
    await page.locator('#binding [data-list-page="next"]').click();
    assert.equal(await page.locator('#binding .app-source-card:visible .app-check:visible').count(), 1);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

function fixtures() {
  const list = (count, fn) => Array.from({ length: count }, (_, index) => fn(index + 1));
  const versions = list(11, version => ({ version, status: 'published', note: '发布' + version }));
  const runs = list(21, id => ({ run_id: 'run-' + id, version: id % 2 + 1, status: 'success', duration_ms: id, updated_at: '2026-10-15T08:00:00Z' }));
  const apps = list(11, id => ({ app_id: id === 1 ? 'enterprise-knowledge-assistant' : 'app-' + id,
    name: '应用' + id, description: '测试应用', status: 'published', enabled: true, version: 1, published_version: 1,
    category: '知识库', type: 'DeepAgent', icon: '知', tags: [], workflow_id: 'workflow-1', workflow_version: 1,
    data_sources: [], tools: [], runtime: { environment: 'production' }, permissions: {}, channels: ['工作台'],
    knowledge_bindings: [], knowledge_binding_mode: 'explicit', versions, run_history: runs }));
  const workflows = list(11, id => ({ workflow_id: 'workflow-' + id, id: 'workflow-' + id, name: '流程' + id,
    status: 'published', version: 1, published_version: 1, enabled: true,
    nodes: [{ id: 'node-1', name: '检索', type: 'unified_search', params: {}, x: 50, y: 50 }], edges: [], versions }));
  const datasets = list(11, id => ({ id: 'dataset-' + id, name: '知识库' + id, effective: true, connected: true }));
  const connections = list(6, id => ({ connection_id: 'connection-' + id, name: 'RAGFlow连接' + id,
    type: 'ragflow', enabled: true, status: 'published', environment: 'production', version: 1, published_version: 1,
    datasets, dataset_ids: datasets.map(item => item.id), published_dataset_ids: datasets.map(item => item.id) }));
  const sources = list(6, id => ({ source_type: ['document', 'graph', 'qa', 'api', 'sql', 'mcp'][id - 1], name: '来源' + id, available: true, configured: true }));
  const tools = list(11, id => ({ tool_id: 'tool-' + id, name: '工具' + id, enabled: true, status: 'published', version: 1,
    published_version: 1, risk: 'read_only', kind: 'http', environment: 'production', input_schema: {}, output_schema: {}, config: {} }));
  const policies = list(11, id => ({ policy_id: 'policy-' + id, name: '策略' + id, category: '知识库', priority: id,
    enabled: true, version: 1, published_version: 1, match: {}, action: { route: 'knowledge_qa' }, versions }));
  const prompts = list(11, id => ({ prompt_id: 'prompt-' + id, name: '提示词' + id, version: 1, published_version: 1,
    model: 'test-model', status: 'published', template: '', prompt_template: '', config: {}, versions }));
  const entries = list(11, id => ({ entry_id: 'entry-' + id, dictionary_type: 'technical_subject',
    canonical_value: '词条' + id, aliases: ['别名' + id], enabled: true, confidence: 0.9 }));
  const dictionaries = list(11, id => ({ dictionary_id: id === 1 ? 'enterprise-default' : 'dict-' + id,
    name: '词典' + id, description: '测试词典', version: 1, published_version: 1, status: 'published', enabled: true,
    entry_count: entries.length, entries, versions }));
  const profiles = list(11, id => ({ profile_id: id === 1 ? 'balanced' : 'profile-' + id, name: '模型' + id,
    model: 'model-' + id, ready: true, enabled: true, capabilities: ['json'], cost_tier: 'medium' }));
  const stages = list(11, id => ({ stage: 'stage-' + id, name: '阶段' + id, required_capabilities: ['json'] }));
  const evalRuns = list(11, id => ({ run_id: 'evaluation-' + id, name: '评测' + id, status: 'success', summary: {} }));
  const results = list(125, id => ({ case_id: 'case-' + id, agent_id: apps[0].app_id, agent_name: '应用1',
    passed: id % 2 === 0, query: '问题' + id, answer: '答案' + id, checks: [], duration_ms: 10 }));
  return { apps, workflows, datasets, connections, sources, tools, policies, prompts, dictionaries, profiles, stages, evalRuns, results };
}

test('production views and dialogs paginate their real record containers', { timeout: 120000 }, async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const data = fixtures();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const pathname = new URL(route.request().url()).pathname;
      const send = value => route.fulfill({ json: value });
      if (pathname === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(staticRoot, 'index_production.html'), 'utf8') });
      if (pathname.startsWith('/assets/')) return route.fulfill({ contentType: pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: fs.readFileSync(path.join(staticRoot, path.basename(pathname)), 'utf8') });
      if (pathname === '/api/tool-center/session') return send({ authenticated: true, principal: { user_id: 'admin', roles: ['admin'], workspaces: ['default'] }, csrf_token: 'test' });
      if (pathname === '/api/tool-center/overview') return send({ tool_count: 11, connection_count: 6, pending_count: 0, call_count: 0 });
      if (pathname === '/api/tool-center/tools' || pathname === '/api/tools/catalog') return send({ tools: data.tools });
      if (pathname === '/api/tool-center/connections') return send({ connections: data.connections });
      if (pathname === '/api/tool-center/credentials') return send({ credentials: data.apps.map((item, index) => ({ credential_id: 'key-' + index, name: item.name, environment: 'production', version: 1, masked: '••••' })) });
      if (pathname === '/api/tool-center/calls') return send({ calls: data.apps.map((item, index) => ({ call_id: 'call-' + index, tool_name: item.name, actor_id: 'admin', status: 'success', environment: 'production', tool_version: 1 })) });
      if (pathname === '/api/apps') return send({ apps: data.apps });
      if (pathname.startsWith('/api/apps/')) return send({ app: data.apps.find(item => item.app_id === pathname.split('/').at(-1)), workflow: data.workflows[0] });
      if (pathname === '/api/workflows') return send({ workflows: data.workflows, templates: [] });
      if (pathname === '/api/workflows/templates') return send({ templates: [] });
      if (pathname.startsWith('/api/workflows/')) return send({ workflow: data.workflows[0], versions: [], templates: [] });
      if (pathname === '/api/search/sources') return send({ sources: data.sources });
      if (pathname === '/api/search') return send({ hits: data.datasets.map(item => ({ title: item.name, content: '内容', source_type: 'document' })) });
      if (pathname === '/api/knowledge/management') return send({ connections: data.connections });
      if (pathname === '/api/knowledge/catalog') return send({ datasets: data.datasets, configured: true });
      if (pathname === '/api/graph/status') return send({ graph: { available: true } });
      if (pathname === '/conversations') return send(data.apps.map(item => ({ conversation_id: item.app_id, title: item.name })));
      if (pathname === '/api/routing-policies') return send({ policies: data.policies });
      if (pathname.startsWith('/api/routing-policies/')) return send({ policy: data.policies[0] });
      if (pathname === '/api/semantic-prompts') return send({ prompts: data.prompts });
      if (pathname.startsWith('/api/semantic-prompts/')) return send({ prompt: data.prompts[0] });
      if (pathname === '/api/model-registry') return send({ profiles: data.profiles, stages: data.stages, routing: [] });
      if (pathname === '/api/dictionaries') return send({ dictionaries: data.dictionaries });
      if (pathname.startsWith('/api/dictionaries/')) return send({ dictionary: data.dictionaries[0] });
      if (pathname === '/api/evaluations/agents') return send({ agents: data.apps.map(item => ({ ...item, status: 'ready' })) });
      if (pathname === '/api/evaluations/datasets') return send({ datasets: [] });
      if (pathname === '/api/evaluations') return send({ runs: data.evalRuns });
      if (pathname.startsWith('/api/evaluations/')) return send({ ...data.evalRuns[0], results: data.results,
        summary: { rankings: data.apps.map((item, index) => ({ agent_id: item.app_id, agent_name: item.name, rank: index + 1 })) } });
      return send({ status: 'ok' });
    });
    await page.goto('http://pagination.test/');
    await page.waitForFunction(() => window.ListPagination && window.ToolCenter);
    await page.evaluate(() => ToolCenter.ready());
    async function open(view, selector, expected = 11) {
      await page.evaluate(view => switchView(view), view);
      await page.waitForSelector(selector);
      await page.waitForFunction(selector => document.querySelector(selector)?.classList.contains('list-page-hidden') === false, selector);
      await page.evaluate(() => ListPagination.refresh());
      assert.equal(await page.locator(selector + ':visible').count(), Math.min(5, expected), view + ': ' + selector);
    }
    await open('apps', '.app-list-item');
    await page.locator('.app-list-panel [data-list-page="next"]').click();
    assert.equal(await page.locator('.app-list-item:visible').first().getAttribute('data-app-select'), 'app-6');
    await page.locator('#app-center-filter').fill('应用11');
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('.app-list-item:visible').count(), 1);
    await page.locator('[data-app-action="open-history"]').click();
    await page.waitForSelector('#app-run-history-dialog[open]');
    assert.equal(await page.locator('[data-history-row]:visible').count(), 5);
    const runPager = page.locator('.app-history-section').last().locator('[data-list-page="next"]');
    for (let index = 0; index < 4; index++) await runPager.click();
    assert.equal(await page.locator('[data-history-row]:visible').count(), 1);
    await page.locator('#app-history-search').fill('run-1');
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('[data-history-row]:visible').count(), 5);
    await page.locator('[data-app-history-close]').click();
    await open('knowledge', '.knowledge-connection', 6);
    assert.equal(await page.locator('.knowledge-connection:visible .knowledge-dataset-check:visible').count(), 25);
    await open('routing', '.rp-list>button');
    await open('prompts', '.sp-list-item');
    await open('models', '.mc-profile-item');
    assert.equal(await page.locator('[data-mc-stage]:visible').count(), 5);
    await open('dictionaries', '.dc-dictionary-item');
    assert.equal(await page.locator('#dc-table-body>tr:visible').count(), 5);
    await page.locator('#dc-entry-search').fill('词条11');
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('#dc-table-body>tr:visible').count(), 1);
    await open('search', '.data-table tbody>tr', 6);
    await page.locator('#sub-search-input').fill('设备查询');
    await page.locator('[data-action="sub-search"]').click();
    await page.waitForSelector('.result-item');
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('.result-item:visible').count(), 5);
    await open('eval', '.eval-history-item');
    await page.locator('.eval-history-item:visible').first().click();
    await page.waitForSelector('.eval-case-detail');
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('.eval-case-detail').count(), 125);
    assert.equal(await page.locator('.eval-case-detail:visible').count(), 5);
    assert.match(await page.locator('.eval-case-list+.list-pagination').textContent(), /共 125 条/);
    await page.locator('#eval-result-filter').selectOption('failed');
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('.eval-case-detail:visible').count(), 5);
    await page.evaluate(() => switchView('tools'));
    await page.waitForSelector('.tc-tabs');
    for (const tab of ['tools', 'connections', 'credentials', 'calls']) {
      await page.locator('[data-tc-tab="' + tab + '"]').click();
      await page.waitForFunction(tab => document.querySelector('[data-tc-tab="' + tab + '"]')?.classList.contains('active'), tab);
      assert.equal(await page.locator('[data-tc-record]:visible').count(), 5, '工具与数据：' + tab);
      await page.locator('[data-tc-action="page-next"]').click();
      assert.equal(await page.locator('[data-tc-record]:visible').count(), tab === 'connections' ? 1 : 5);
    }
    await page.evaluate(() => switchView('graph-pipeline'));
    await page.waitForSelector('#graph-preview');
    await page.evaluate(() => renderGraphPreview({
      entities: Array.from({ length: 6 }, (_, index) => ({ id: 'entity-' + index, name: '实体' + index, type: 'Device' })),
      relations: Array.from({ length: 6 }, (_, index) => ({ source_id: 'entity-0', target_id: 'entity-' + index, type: 'HAS', properties: {} })),
    }));
    await page.evaluate(() => ListPagination.refresh());
    assert.equal(await page.locator('.review-table tbody>tr:visible').count(), 5);
    assert.equal(await page.locator('.review-table input:checked').count(), 12);
    await page.evaluate(() => switchView('workflow'));
    await page.waitForSelector('#workflow-run-log');
    await page.evaluate(() => {
      state.workflowLogs = Array.from({ length: 11 }, (_, index) => ({ name: '节点' + index, status: 'success' }));
      document.getElementById('workflow-run-log').innerHTML = workflowRunLogMarkup();
      ListPagination.refresh();
    });
    assert.equal(await page.locator('#workflow-run-log>div:visible').count(), 5);
    for (const view of ['devices', 'tasks', 'logs', 'settings']) await open(view, '.card-grid>.info-card', 2);
    await page.evaluate(() => {
      switchView('chat');
      renderSources({ citations: Array.from({ length: 13 }, (_, index) => ({ document_id: 'doc-' + index, document_name: '文档' + index })) });
      ListPagination.refresh();
    });
    assert.equal(await page.locator('#sources-list>.source-card').count(), 13);
    assert.equal(await page.locator('#sources-list>.source-card:visible').count(), 5);
    assert.equal(await page.locator('#conversation-list>.conversation:visible').count(), 5);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

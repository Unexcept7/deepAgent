const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { chromium } = require('playwright');

const staticRoot = path.join(__dirname, '../../app/static');

function bootstrap() {
  window.viewMeta = { tools: {} };
  window.state = {};
  window.renderTools = async () => '';
  window.renderResult = () => {};
  window.appToolOptions = () => '';
  window.appCollectPayload = () => ({});
  window.fetchJson = async (url, options) => (await fetch(url, options)).json();
  window.subviewShell = (view, body, actions = '') => '<div class="subview-head"><div><h1>工具与数据</h1><p>' +
    viewMeta.tools.desc + '</p></div><div class="subview-actions">' + actions + '</div></div>' + body;
  window.fullRenders = 0;
  window.renderSubview = async () => {
    window.fullRenders += 1;
    const root = document.getElementById('subview');
    root.innerHTML = '<div>正在加载页面数据…</div>';
    root.innerHTML = await window.renderTools();
  };
}

test('tool center keeps details local and fits five-row lists to desktop viewports', { timeout: 60000 }, async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const requests = [];
    const errors = [];
    let failSave = false;
    page.on('pageerror', error => errors.push(error.message));
    const tools = Array.from({ length: 8 }, (_, index) => ({
      tool_id: 'tool_' + (index + 1), name: '工具' + (index + 1), description: '设备状态查询',
      kind: 'http', environment: 'production', risk: 'read_only', method: 'GET', path: '/status',
      input_schema: { type: 'object', properties: {} }, output_schema: { type: 'object' },
      config: {}, allowed_roles: ['admin'], enabled: true, version: 1, published_version: 1,
      status: 'published', timeout_seconds: 8, rate_limit_per_minute: 30,
    }));
    const connections = Array.from({ length: 6 }, (_, index) => ({
      connection_id: 'conn_' + (index + 1), name: '连接' + (index + 1), type: 'business_api',
      endpoint: 'http://127.0.0.1:8100', auth_mode: 'none', auth_header: 'X-API-Key',
      health_path: '/health', timeout_seconds: 8, environment: 'production', config: {},
      enabled: true, version: 1, published_version: 1, status: 'published',
    }));
    const credentials = Array.from({ length: 6 }, (_, index) => ({ credential_id: 'key_' + (index + 1), name: '测试凭据' + (index + 1), environment: 'production',
      version: 1, source: 'encrypted', masked: '••••••', updated_at: '2026-10-15' }));
    const calls = Array.from({ length: 6 }, (_, index) => ({ call_id: 'call_' + (index + 1), tool_id: 'tool_' + (index + 1), tool_name: '工具' + (index + 1), actor_id: 'admin',
      status: 'success', environment: 'production', tool_version: 1, created_at: '2026-10-15T08:00:00Z', duration_ms: 124,
      parameters: {}, result: { ok: true } }));

    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;
      const send = (data, status = 200) => route.fulfill({ status, json: data });
      if (pathname === '/') {
        return route.fulfill({ contentType: 'text/html', body: '<html><head><meta charset="UTF-8"><link rel="stylesheet" href="/assets/tool-center.css"></head><body>' +
          '<main id="main" style="height:820px;overflow:auto"><div style="height:120px"></div><div id="subview"></div><div style="height:500px"></div></main>' +
          '<script>(' + bootstrap.toString() + ')();</script><script src="/assets/tool-center.js"></script></body></html>' });
      }
      if (pathname.startsWith('/assets/')) {
        return route.fulfill({ contentType: pathname.endsWith('.js') ? 'text/javascript' : 'text/css',
          body: fs.readFileSync(path.join(staticRoot, path.basename(pathname)), 'utf8') });
      }
      if (!pathname.startsWith('/api/')) return route.fulfill({ status: 404, body: '' });
      requests.push(route.request().method() + ' ' + pathname);
      if (pathname === '/api/tool-center/session') return send({ authenticated: true, csrf_token: 'test-csrf',
        principal: { user_id: 'admin', roles: ['admin'], workspaces: ['default'] }, local_login_available: false });
      if (pathname === '/api/apps') return send({ apps: [] });
      if (pathname === '/api/tool-center/overview') return send({ tool_count: tools.length,
        connection_count: connections.length, call_count: calls.length, pending_count: 0, warnings: [] });
      if (pathname === '/api/tool-center/credentials') return send({ credentials });
      if (pathname === '/api/tool-center/calls') return send({ calls });
      const collection = pathname.endsWith('/tools') ? tools : pathname.endsWith('/connections') ? connections : null;
      if (collection && route.request().method() === 'GET') return send(collection === tools ? { tools } : { connections });
      if (collection && route.request().method() === 'POST') {
        if (failSave) return send({ detail: '测试保存失败' }, 409);
        const data = route.request().postDataJSON();
        const key = collection === tools ? 'tool_id' : 'connection_id';
        const resource = collection.find(item => item[key] === data[key]);
        Object.assign(resource, data, { version: resource.version + 1, status: 'draft' });
        return send(collection === tools ? { tool: resource } : { connection: resource });
      }
      const match = pathname.match(/^\/api\/tool-center\/resources\/(tools|connections)\/([^/]+)(\/publish)?$/);
      if (match) {
        const list = match[1] === 'tools' ? tools : connections;
        const resource = list.find(item => (item.tool_id || item.connection_id) === match[2]);
        if (match[3]) Object.assign(resource, { status: 'published', published_version: resource.version });
        return send({ resource: { ...resource, versions: [{ version: resource.version }] } });
      }
      return send({ detail: 'Unexpected test request: ' + pathname }, 500);
    });

    await page.goto('http://tool-center.test/');
    await page.evaluate(async () => { await ToolCenter.ready(); await renderSubview('tools'); });
    assert.equal(await page.locator('[data-tc-record]').count(), 5);
    await page.locator('[data-tc-action="page-next"]').click();
    assert.equal(await page.locator('[data-tc-record]').count(), 3);
    await page.evaluate(() => {
      window.retained = { head: document.querySelector('.subview-head'), filter: document.getElementById('tc-filter'),
        panel: document.querySelector('.tc-list-panel') };
      document.getElementById('main').scrollTop = 120;
    });
    const initialScroll = await page.locator('#main').evaluate(node => node.scrollTop);
    let start = requests.length;
    await page.locator('[data-tc-action="select"][data-tc-id="tool_6"]').evaluate(node => node.click());
    await page.waitForSelector('.tc-modal');
    assert.equal(await page.locator('.tc-modal-head').evaluate(node => getComputedStyle(node).position), 'sticky');
    assert.deepEqual(requests.slice(start), ['GET /api/tool-center/resources/tools/tool_6']);
    assert.equal(await page.locator('#main').evaluate(node => node.scrollTop), initialScroll);

    await page.locator('#tc-name').fill('工具6已更新');
    await page.locator('.tc-modal .tc-advanced').first().evaluate(node => { node.open = true; });
    await page.locator('.tc-modal-dialog').evaluate(node => {
      window.retained.dialog = node;
      node.scrollTop = 230;
    });
    const modalScroll = await page.locator('.tc-modal-dialog').evaluate(node => node.scrollTop);
    await page.locator('#tc-editor button[type="submit"]').evaluate(node => node.click());
    await page.waitForFunction(() => document.querySelector('[data-tc-record="tool_6"]').textContent.includes('工具6已更新') &&
      document.querySelector('[data-tc-record="tool_6"]').textContent.includes('v2'));
    assert.equal(await page.locator('.tc-modal-title').textContent(), '工具6已更新');
    assert.match(await page.locator('.tc-pagination').textContent(), /第 2 \/ 2 页/);
    assert.equal(await page.locator('.tc-modal-dialog').evaluate(node => node === retained.dialog), true);
    assert.equal(await page.locator('.tc-modal-dialog').evaluate(node => node.scrollTop), modalScroll);
    assert.equal(await page.locator('.tc-modal .tc-advanced').first().evaluate(node => node.open), true);
    assert.equal(await page.locator('#main').evaluate(node => node.scrollTop), initialScroll);

    failSave = true;
    await page.locator('#tc-name').fill('未保存的修改');
    await page.locator('#tc-editor button[type="submit"]').evaluate(node => node.click());
    await page.waitForSelector('.tc-message.error');
    assert.equal(await page.locator('#tc-name').inputValue(), '未保存的修改');
    failSave = false;
    start = requests.length;
    await page.locator('.tc-modal-head [data-tc-action="close-editor"]').evaluate(node => node.click());
    assert.equal(await page.locator('.tc-modal').count(), 0);
    await page.locator('[data-tc-action="new"]').click();
    assert.match(await page.locator('.tc-pagination').textContent(), /第 2 \/ 2 页/);
    await page.locator('.tc-modal-head [data-tc-action="close-editor"]').click();
    await page.locator('#tc-filter').focus();
    await page.keyboard.type('工具6');
    assert.equal(await page.locator('[data-tc-record]').count(), 1);
    assert.equal(await page.locator('#tc-filter').evaluate(node => node === document.activeElement), true);
    assert.deepEqual(requests.slice(start), []);
    assert.equal(await page.evaluate(() => fullRenders), 1);
    assert.equal(await page.evaluate(() => Object.values(retained).every(node => node === retained.dialog || node.isConnected)), true);

    await page.locator('#tc-filter').fill('');
    await page.locator('[data-tc-tab="connections"]').click();
    await page.waitForSelector('[data-tc-record="conn_1"]');
    await page.locator('[data-tc-id="conn_1"][data-tc-action="select"]').click();
    await page.waitForSelector('.tc-modal');
    start = requests.length;
    await page.locator('#tc-endpoint').fill('http://127.0.0.1:8100/mcp');
    await page.locator('#tc-type').selectOption('mcp');
    assert.equal(await page.locator('#tc-endpoint').inputValue(), 'http://127.0.0.1:8100/mcp');
    assert.equal(await page.locator('#tc-mcp-protocol').count(), 1);
    assert.deepEqual(requests.slice(start), []);
    assert.equal(await page.evaluate(() => fullRenders), 2);
    assert.deepEqual(errors, []);

    await page.locator('.tc-modal-head [data-tc-action="close-editor"]').click();
    const productionHtml = fs.readFileSync(path.join(staticRoot, 'index_production.html'), 'utf8');
    await page.addStyleTag({ content: productionHtml.match(/<style>([\s\S]*?)<\/style>/)[1] });
    await page.addStyleTag({ path: path.join(staticRoot, 'tool-center.css') });
    await page.addStyleTag({ path: path.join(staticRoot, 'workbench-theme.css') });
    await page.evaluate(() => {
      const subview = document.getElementById('subview');
      document.body.innerHTML = '<div class="app"><aside class="sidebar"><nav class="nav"><button data-view="tools" class="active">工具与数据</button></nav></aside>' +
        '<section class="main"><header class="topbar">工具与数据 / 企业知识助手</header><main class="workspace-main"></main></section></div>';
      subview.style.display = 'block';
      document.querySelector('.workspace-main').appendChild(subview);
    });
    const viewports = [{ width: 1920, height: 1080 }, { width: 1657, height: 880 },
      { width: 1366, height: 768 }, { width: 1280, height: 720 }, { width: 1024, height: 768 },
      { width: 900, height: 640 }];
    for (const tab of ['connections', 'tools', 'credentials', 'calls']) {
      await page.locator('[data-tc-tab="' + tab + '"]').click();
      await page.waitForFunction(tab => document.querySelector('[data-tc-tab="' + tab + '"]')?.classList.contains('active'), tab);
      const count = { connections: 6, tools: 8, credentials: 6, calls: 6 }[tab];
      assert.equal(await page.locator('[data-tc-tab="' + tab + '"]').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('[data-tc-count="' + tab + '"]').textContent(), String(count));
      assert.match(await page.locator('.tc-pagination').textContent(), new RegExp('第 1 / ' + (count > 5 ? 2 : 1) + ' 页 · 共 ' + count + ' 条'));
      assert.equal(await page.locator('[data-tc-action="page-next"]').isDisabled(), count <= 5);
      for (const viewport of viewports) {
        await page.setViewportSize(viewport);
        const layout = await page.evaluate(async () => {
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          const main = document.querySelector('.workspace-main');
          const panel = document.querySelector('.tc-list-panel');
          const pager = document.querySelector('.tc-pagination');
          const table = document.querySelector('.tc-table-wrap');
          const rows = [...document.querySelectorAll('[data-tc-record]')];
          return { mainHeight: main.clientHeight, mainContent: main.scrollHeight,
            tableHeight: table.clientHeight, tableContent: table.scrollHeight,
            panelBottom: panel.getBoundingClientRect().bottom,
            contentBottom: main.getBoundingClientRect().bottom - parseFloat(getComputedStyle(main).paddingBottom),
            pagerBottom: pager?.getBoundingClientRect().bottom, pagerTop: pager?.getBoundingClientRect().top,
            lastRowBottom: rows.at(-1).getBoundingClientRect().bottom, rowCount: rows.length };
        });
        const label = tab + ' ' + viewport.width + 'x' + viewport.height + ': ' + JSON.stringify(layout);
        assert.ok(layout.mainContent <= layout.mainHeight + 1, label);
        assert.ok(layout.tableContent <= layout.tableHeight + 1, label);
        assert.ok(Math.abs(layout.panelBottom - layout.contentBottom) <= 2, label);
        if (layout.pagerBottom) {
          assert.ok(layout.pagerBottom <= viewport.height, label);
          assert.ok(layout.lastRowBottom <= layout.pagerTop, label);
        }
        assert.equal(layout.rowCount, 5);
        if (tab === 'connections' && viewport.width === 1366) {
          await page.screenshot({ path: path.join(require('node:os').tmpdir(), 'deepagent-tool-center-1366x768.png') });
        }
        if (process.env.TOOL_CENTER_SCREENSHOTS && [1366, 900].includes(viewport.width)) {
          fs.mkdirSync(process.env.TOOL_CENTER_SCREENSHOTS, { recursive: true });
          await page.screenshot({ path: path.join(process.env.TOOL_CENTER_SCREENSHOTS, tab + '-' + viewport.width + 'x' + viewport.height + '.png') });
        }
      }
    }
    await page.locator('[data-tc-tab="tools"]').click();
    await page.waitForSelector('[data-tc-record="tool_1"]');
    await page.locator('#tc-filter').fill('不存在的资源');
    assert.equal(await page.locator('.tc-empty strong').textContent(), '未找到匹配记录');
    assert.match(await page.locator('.tc-pagination').textContent(), /共 0 条/);
    await page.locator('#tc-filter').fill('');
    await page.locator('.tc-more>summary').click();
    assert.equal(await page.locator('[data-tc-action="import"]').isVisible(), true);
    assert.equal(await page.locator('[data-tc-action="export"]').isVisible(), true);
    await page.locator('[data-tc-action="import"]').click();
    await page.waitForSelector('.tc-import');
    await page.locator('[data-tc-action="close-import"]').click();
    await page.waitForSelector('[data-tc-record="tool_1"]');
    await page.setViewportSize({ width: 1366, height: 768 });
    await page.locator('[data-tc-id="tool_1"][data-tc-action="select"]').click();
    await page.waitForSelector('.tc-modal');
    if (process.env.TOOL_CENTER_SCREENSHOTS) {
      await page.screenshot({ path: path.join(process.env.TOOL_CENTER_SCREENSHOTS, 'tool-detail-1366x768.png') });
    }
    await page.locator('.tc-modal-head [data-tc-action="close-editor"]').click();
    await page.evaluate(() => {
      document.querySelector('[data-view="tools"]').classList.remove('active');
      document.getElementById('subview').style.display = 'none';
    });
    assert.equal(await page.locator('#subview').evaluate(node => getComputedStyle(node).display), 'none');
  } finally {
    await browser.close();
  }
});

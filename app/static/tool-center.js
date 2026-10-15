(function () {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  const PAGE_SIZE = 5;
  const ui = {tab: 'tools', selected: null, isNew: false, modalOpen: false, page: 1, filter: '', environment: '', status: '', resource: null, importOpen: false, preview: null, lastCall: null, invokeKey: null, invokeBody: null};
  const auth = {principal: null, csrf: null, local: false, workspace: localStorage.getItem('deepagent_tool_workspace') || 'default'};
  const e = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const labels = {read_only:'只读',draft:'本地草稿',confirm:'本人确认',approval:'不同人员审批',device_control:'设备控制审批',healthy:'测试通过',unknown:'未测试/结果不确定',local:'本地处理',
    error:'失败',success:'成功',published:'已发布',disabled:'已停用',running:'执行中',awaiting_confirmation:'等待本人确认',awaiting_approval:'等待其他人员审批',expired:'已过期',rejected:'已拒绝',test:'测试',production:'生产'};
  const status = value => '<span class="tc-status ' + e(value) + '">' + e(labels[value] || value || '未知') + '</span>';
  const json = value => '<pre class="tc-json">' + e(JSON.stringify(value, null, 2)) + '</pre>';
  const has = roles => !!auth.principal && roles.some(role => auth.principal.roles.includes(role));
  const manage = () => has(['admin','editor']);
  const admin = () => has(['admin']);
  const btn = (action, text, attrs, primary, disabled) => '<button type="button" class="tc-btn' + (primary ? ' primary' : '') + '" data-tc-action="' + e(action) + '" ' + (attrs || '') + (disabled ? ' disabled' : '') + '>' + e(text) + '</button>';
  const input = (id, label, value, attrs) => '<label class="tc-field"><span>' + e(label) + '</span><input id="tc-' + id + '" value="' + e(value) + '" ' + (attrs || '') + '></label>';
  const area = (id, label, value, schema) => '<label class="tc-field"><span>' + e(label) + '</span><textarea id="tc-' + id + '" ' + (schema ? 'class="tc-schema"' : '') + '>' + e(typeof value === 'string' ? value : JSON.stringify(value || {},null,2)) + '</textarea></label>';
  const select = (id, label, value, choices) => '<label class="tc-field"><span>' + e(label) + '</span><select id="tc-' + id + '">' + choices.map(item => '<option value="' + e(item[0]) + '" ' + (item[0] === value ? 'selected' : '') + '>' + e(item[1]) + '</option>').join('') + '</select></label>';
  const value = id => document.getElementById('tc-' + id)?.value || '';
  const parse = id => {try{return JSON.parse(value(id) || '{}')}catch(err){throw new Error('“' + id + '”不是有效 JSON')}};
  const canonical = id => ({device_status:'get_device_status',create_work_order:'submit_work_order'}[id] || id);
  const tabs = {tools:'工具',connections:'数据连接',credentials:'凭据',calls:'调用记录'};
  const icons = {
    tools:'<path d="m14.5 6.5 3 3 3.3-3.3a6 6 0 0 1-7.6 7.6l-6.5 6.5a2.1 2.1 0 0 1-3-3l6.5-6.5a6 6 0 0 1 7.6-7.6z"/>',
    connections:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/><path d="M14 6h3a1 1 0 0 1 1 1v7M6 10v7a1 1 0 0 0 1 1h7"/>',
    credentials:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 4v3"/>',
    calls:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    pending:'<path d="M9 3h6m-7 0H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-2M9 3v4h6V3m-7 11 3 3 5-6"/>',
    search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>'
  };
  const icon = name => '<svg class="tc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + icons[name] + '</svg>';
  function dateLabel(value) {
    if(!value)return '—';
    if(/^\d{4}-\d{2}-\d{2}$/.test(value))return value.replace(/-/g,'/');
    const date=new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  }
  function message(text, error) {
    document.querySelector('.tc-message')?.remove();
    const box = document.createElement('div'); box.className = 'tc-message' + (error ? ' error' : ''); box.textContent = text;
    document.body.appendChild(box); setTimeout(() => box.remove(), 6000);
  }
  async function rawJson(path, options) {
    const response = await nativeFetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败：HTTP ' + response.status);
    return data;
  }
  function setAuth(data) {
    auth.principal = data.principal; auth.csrf = data.csrf_token;
    if (auth.principal && !auth.principal.workspaces.includes(auth.workspace)) auth.workspace = auth.principal.workspaces[0];
    localStorage.setItem('deepagent_tool_workspace', auth.workspace);
    const workspaceLabel=document.querySelector('#workspace strong');
    if(workspaceLabel)workspaceLabel.textContent=auth.principal ? auth.workspace : '请登录并选择工作区';
    const userLabel=document.querySelector('.sidebar-footer .user span');
    if(userLabel)userLabel.textContent=auth.principal?.user_id || '未登录';
  }
  async function startAuth() {
    try {
      const data = await rawJson('/api/tool-center/session'); auth.local = data.local_login_available;
      setAuth(data);
      if (!data.authenticated && auth.local) setAuth(await rawJson('/api/tool-center/session/local', {method:'POST',headers:{'X-Tool-Center-Login':'1','Content-Type':'application/json'},body:'{}'}));
    } catch (error) { console.warn('工具中心身份初始化失败', error.message); }
  }
  let ready = startAuth();
  window.fetch = async function (resource, options) {
    const url = new URL(typeof resource === 'string' ? resource : resource.url, location.href);
    if (url.origin !== location.origin) return nativeFetch(resource, options);
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/chat')) {
      await ready;
      const next = Object.assign({}, options || {});
      const headers = new Headers(options?.headers || (resource instanceof Request ? resource.headers : undefined));
      headers.set('X-Workspace-Id', auth.workspace);
      if (auth.csrf) headers.set('X-Tool-Center-CSRF', auth.csrf);
      next.headers = headers;
      return nativeFetch(resource, next);
    }
    return nativeFetch(resource, options);
  };
  async function api(path, body, method) {
    const response = await fetch('/api/tool-center' + path, {method:method || (body === undefined ? 'GET' : 'POST'),
      headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '参数校验失败：HTTP ' + response.status);
    return data;
  }
  function loginMarkup() {
    return '<section class="tc-panel tc-login"><h2>登录工具与数据中心</h2><div class="tc-note">身份由服务器验证，不能通过修改浏览器角色获得权限。企业部署请由管理员分配身份令牌。</div>' +
      input('login-token','企业身份令牌','','type="password" autocomplete="off"') + '<div class="tc-actions">' + btn('login','登录',null,true) +
      (auth.local ? btn('local-login','本地开发管理员登录') : '') + '</div><div id="tc-login-error"></div></section>';
  }
  function tabsMarkup() {
    return '<div class="tc-tabs" aria-label="资源分类">' + Object.entries(tabs).map(([id,label]) =>
      '<button type="button" class="tc-tab ' + (ui.tab === id ? 'active' : '') + '" data-tc-tab="' + id + '" aria-pressed="' + (ui.tab===id) + '">' + icon(id) + label +
      '<span class="tc-tab-count" data-tc-count="' + id + '">' + (ui[id] || []).length + '</span></button>').join('') + '</div>';
  }
  function sessionMarkup() {
    return '<div class="tc-session"><span class="tc-user" title="' + e(auth.principal.roles.join(' / ')) + '">' + e(auth.principal.user_id) + '</span>' +
      '<label class="tc-workspace"><span>工作区</span><select id="tc-workspace" aria-label="工作区">' + auth.principal.workspaces.map(id => '<option ' + (id === auth.workspace ? 'selected' : '') + ' value="' + e(id) + '">' + e(id) + '</option>').join('') + '</select></label>' + btn('logout','退出登录') + '</div>';
  }
  function filtersMarkup() {
    const placeholders={tools:'搜索工具名称、ID 或用途',connections:'搜索连接名称、ID 或说明',credentials:'搜索凭据名称或 ID',calls:'搜索工具 ID、调用 ID 或用户'};
    return '<div class="tc-toolbar"><label class="tc-search">' + icon('search') + '<input id="tc-filter" type="search" aria-label="搜索记录" placeholder="' + placeholders[ui.tab] + '" value="' + e(ui.filter) + '"></label>' +
      '<select id="tc-filter-env" aria-label="筛选环境"><option value="">所有环境</option>' + ['test','production'].map(id => '<option value="' + id + '" ' + (ui.environment === id ? 'selected' : '') + '>' + labels[id] + '</option>').join('') + '</select>' +
      (ui.tab === 'calls' ? '<select id="tc-filter-status" aria-label="筛选调用状态"><option value="">所有状态</option>' + ['success','error','unknown','awaiting_confirmation','awaiting_approval','expired','rejected','running'].map(id => '<option value="' + id + '" ' + (ui.status === id ? 'selected' : '') + '>' + labels[id] + '</option>').join('') + '</select>' : '') + btn('refresh','刷新') + '</div>';
  }
  function matches(item) {
    return (!ui.environment || item.environment === ui.environment) && (!ui.status || ui.tab !== 'calls' || item.status === ui.status) &&
      (!ui.filter || JSON.stringify([item.name,item.tool_id,item.connection_id,item.credential_id,item.description,item.actor_id,item.call_id]).toLowerCase().includes(ui.filter.toLowerCase()));
  }
  function paginate(items) {
    const filtered=items.filter(matches); const pages=Math.max(1,Math.ceil(filtered.length/PAGE_SIZE));
    ui.page=Math.min(Math.max(1,ui.page),pages);
    const start=(ui.page-1)*PAGE_SIZE;
    return {items:filtered.slice(start,start+PAGE_SIZE),total:filtered.length,pages};
  }
  function pagination(info) {
    return '<div class="tc-pagination"><span>第 ' + ui.page + ' / ' + info.pages + ' 页 · 共 ' + info.total + ' 条</span><div class="tc-actions">' +
      btn('page-prev','上一页',null,false,ui.page<=1) + btn('page-next','下一页',null,false,ui.page>=info.pages) + '</div></div>';
  }
  function recordsMarkup() {
    const page=paginate(ui[ui.tab] || []);
    const rows=ui.tab==='credentials' ? credentialTable(page.items) : ui.tab==='calls' ? callsTable(page.items) : resourcesTable(page.items);
    return rows + pagination(page);
  }
  function updateRecords() {
    const target=document.getElementById('tc-records');if(!target)return;
    target.innerHTML=recordsMarkup();bind(target);
  }
  function table(headers, rows) {
    const filtered=!!(ui.filter || ui.environment || (ui.tab==='calls' && ui.status));
    const canCreate=ui.tab==='credentials' ? admin() : manage();
    const empty='<div class="tc-empty">' + icon(ui.tab) + '<strong>' + (filtered ? '未找到匹配记录' : '暂无' + tabs[ui.tab]) + '</strong><p>' +
      (filtered ? '试试其他关键词，或调整筛选条件。' : ui.tab==='calls' ? '工具执行后，可在这里查看调用状态与结果。' : canCreate ? '点击右上角“新建' + tabs[ui.tab] + '”开始配置。' : '当前工作区还没有可用资源。') + '</p></div>';
    return '<div class="tc-table-wrap"><table class="tc-table"><thead><tr>' + headers.map(text => '<th>' + text + '</th>').join('') + '</tr></thead><tbody>' +
      (rows.length ? rows.join('') : '<tr><td colspan="' + headers.length + '">' + empty + '</td></tr>') + '</tbody></table></div>';
  }
  function resourcesTable(items) {
    const tool = ui.tab === 'tools';
    return table([tool ? '工具名称' : '连接名称','版本与环境',tool ? '风险控制' : '连接状态','操作'],
      items.map(item => {
        const id = item[tool ? 'tool_id' : 'connection_id'];
        const health = item.health || {};
        const description=[id,tool ? item.description : item.endpoint || item.config?.path || '地址未配置'].filter(Boolean).join(' · ');
        return '<tr data-tc-record="' + e(id) + '" class="' + (id === ui.selected ? 'selected' : '') + '"><td><div class="tc-resource"><span class="tc-resource-icon">' + icon(ui.tab) + '</span><div class="tc-resource-copy"><b title="' + e(item.name) + '">' + e(item.name) + '</b><small title="' + e(description) + '">' + e(description) + '</small></div></div></td><td><span class="tc-version">v' + item.version + '</span> ' + status(item.status) + '<small>' +
          e(labels[item.environment]) + ' · ' + e(item.published_version ? '运行 v' + item.published_version : '尚未发布') + '</small></td><td>' +
          (tool ? status(item.risk) + '<small>' + e(labels[item.execution_status] || item.execution_status) + '</small>' : (!health.status || (health.status==='unknown' && !health.checked_at) ? '<span class="tc-status">未测试</span>' : status(health.status)) + '<small title="' + e(health.error || health.checked_at || '') + '">' + e(health.error || (health.checked_at ? dateLabel(health.checked_at) : '尚未进行连接测试')) + '</small>') +
          '</td><td class="tc-nowrap">' + btn('select','详情','data-tc-id="' + e(id) + '"') +
          (!tool ? ' ' + btn('test','测试','data-tc-id="' + e(id) + '"',false,!manage()) : '') + '</td></tr>';
      }));
  }
  function versionActions(resource) {
    if (!resource?.version) return '';
    return '<details class="tc-advanced"><summary>高级：启停、历史版本与回滚</summary><div class="tc-actions">' + btn('toggle',resource.enabled ? '停用' : '启用',null,false,!manage()) +
      '</div><label class="tc-field"><span>历史版本（回滚会生成并发布新版本）</span><select id="tc-history">' + (resource.versions || []).map(item => '<option value="' + item.version + '">v' + item.version + ' · ' + e(item.note || item.created_at) + '</option>').join('') +
      '</select></label><div class="tc-actions">' + btn('rollback','回滚并发布',null,false,!admin()) + btn('compare','与当前版本比较') + '</div><div id="tc-diff"></div></details>';
  }
  function sample(schema) {
    const result = {};
    for (const key of schema.required || []) {
      const prop = schema.properties?.[key] || {};
      result[key] = prop.default ?? prop.enum?.[0] ?? (prop.type === 'integer' || prop.type === 'number' ? prop.minimum ?? 1 : prop.type === 'boolean' ? false : prop.type === 'object' ? {} : prop.type === 'array' ? [] : key === 'device_id' ? 'B47-001' : key === 'problem' ? '请输入真实问题' : '');
    }
    return result;
  }
  function toolEditor(resource) {
    const r = resource || {tool_id:'',name:'',description:'',kind:'http',environment:'test',connection_id:'',risk:'read_only',method:'GET',path:'/',timeout_seconds:8,rate_limit_per_minute:30,
      allowed_roles:['admin','operator','approver'],input_schema:{type:'object',properties:{device_id:{type:'string',minLength:1}},required:['device_id'],additionalProperties:false},output_schema:{type:'object'},config:{}};
    const connections = (ui.connections || []).filter(item => ['mcp','business_api'].includes(item.type));
    const apps = ui.apps || [];
    const saveText = admin() ? '保存并发布' : '保存草稿';
    return '<section class="tc-panel tc-editor"><h2>' + (ui.isNew ? '新建工具' : '工具定义与调试') + '</h2>' +
      '<form id="tc-editor"><div class="tc-fields">' + input('id','工具 ID',r.tool_id, ui.isNew ? 'required maxlength="64"' : 'readonly') + input('name','显示名称',r.name,'required maxlength="160"') +
      select('kind','实现类型',r.kind,[['http','HTTP API'],['builtin','本地工单草稿'],['mcp','MCP HTTP 工具']]) +
      select('env','环境',r.environment,[['test','测试'],['production','生产']]) + select('risk','风险控制',r.risk,Object.keys(labels).filter(id => ['read_only','draft','confirm','approval','device_control'].includes(id)).map(id => [id,labels[id]])) + '</div>' +
      area('description','用途说明（提供给 Agent）',r.description) +
      select('connection','数据连接',r.connection_id || '',[['','不使用外部连接'],...connections.map(item => [item.connection_id,item.name + ' · ' + labels[item.environment]])]) +
      '<details class="tc-advanced"><summary>高级：执行参数与 JSON Schema</summary>' + input('owner','负责人',r.owner || '') + '<div class="tc-fields">' + select('method','HTTP 方法',r.method,['GET','HEAD','POST','PUT','PATCH','DELETE'].map(id => [id,id])) + input('path','相对路径',r.path || '/') +
      input('handler','内置处理器',r.handler || (r.kind === 'builtin' ? 'draft_work_order' : '')) + input('permission','权限标识',r.required_permission || 'tool:invoke') +
      input('timeout','超时（秒）',r.timeout_seconds,'type="number" min="1" max="60"') + input('rate','每用户每分钟调用上限',r.rate_limit_per_minute,'type="number" min="1" max="1000"') + '</div>' +
      input('roles','可调用角色（逗号分隔）',(r.allowed_roles || []).join(',')) + area('input-schema','输入 JSON Schema',r.input_schema,true) + area('output-schema','输出 JSON Schema',r.output_schema,true) + area('config','非敏感执行配置',r.config) + '</details>' +
      '<div class="tc-note">MCP 只读工具通常不需要修改 Schema；秘密只通过连接凭据注入。管理员点击“保存并发布”后立即对应用可用。</div>' +
      '<button type="submit" class="tc-btn primary" ' + (!manage() ? 'disabled' : '') + '>' + saveText + '</button></form>' + versionActions(r) +
      (!ui.isNew && r.version ? '<h3>真实调用 / 单工具调试</h3>' + select('invoke-app','正式调用的 Agent',apps[0]?.app_id || '',[['','请选择已发布的应用'],...apps.map(item => [item.app_id,item.name])]) +
      area('parameters','调用参数（示例占位，执行前请核对）',sample(r.input_schema)) + '<div class="tc-actions">' + btn('debug','调试当前草稿',null,false,!manage()) + btn('invoke','按 Agent 授权正式调用',null,true,!has(['admin','operator','approver'])) +
      btn('new-invocation','新一轮调用',null,false,!has(['admin','operator','approver','editor'])) + '</div><div id="tc-invoke-result">' + (ui.lastCall ? callCard(ui.lastCall) : '') + '</div>' +
      '<h3>Agent 授权</h3><div class="tc-note">授权修改会保存为应用草稿。“授权并发布”会发布该应用当前版本的所有配置。</div>' +
      select('bind-app','应用',apps[0]?.app_id || '',apps.map(item => [item.app_id,item.name])) + '<div class="tc-actions">' + btn('bind','授权并发布应用',null,false,!admin()) + btn('unbind','移除授权并发布',null,false,!admin()) +
      '</div><div class="tc-note">当前绑定：' + e(apps.filter(app => (app.tools || []).some(item => canonical(item.tool_id) === r.tool_id && item.enabled !== false)).map(app => app.name).join('、') || '无') + '</div>' : '') + '</section>';
  }
  function readConnectionConfig() {
    if (value('type') === 'mcp') {
      const config = {transport:'streamable_http',protocol_version:value('mcp-protocol') || '2025-11-25'};
      const searchTool = value('search-tool').trim();
      if (searchTool) config.search_tool = searchTool;
      return config;
    }
    return parse('config');
  }
  function connectionEditor(resource) {
    const r = resource || {connection_id:'',name:'',type:'business_api',environment:'test',endpoint:'',auth_mode:'none',auth_header:'X-API-Key',health_path:'/health',timeout_seconds:8,config:{}};
    const saveText = admin() ? '保存并发布' : '保存草稿';
    const mcp = r.type === 'mcp';
    const mcpConfig = '<div class="tc-mcp-config"><div class="tc-fields">' + select('mcp-protocol','MCP 协议版本',r.config?.protocol_version || '2025-11-25',[['2025-11-25','2025-11-25'],['2025-06-18','2025-06-18'],['2025-03-26','2025-03-26'],['2026-07-28','2026-07-28']]) +
      input('search-tool','统一搜索工具（导入后填写）',r.config?.search_tool || '','placeholder="例如 equipment_search"') + '</div><div class="tc-note">传输方式固定为 Streamable HTTP；导入并发布只读工具后，再填写远程工具名即可启用统一搜索。</div></div>';
    const otherConfig = area('config','非敏感连接配置',r.config);
    return '<section class="tc-panel tc-editor"><h2>' + (ui.isNew ? '新建数据连接' : '连接定义') + '</h2><form id="tc-editor">' +
      '<div class="tc-fields">' + input('id','连接 ID',r.connection_id,ui.isNew ? 'required maxlength="100"' : 'readonly') + input('name','显示名称',r.name,'required') +
      select('type','类型',r.type,[['business_api','业务 API'],['ragflow','RAGFlow'],['neo4j','Neo4j'],['sql','只读 SQL 搜索网关'],['mcp','MCP HTTP / 搜索代理'],['qa','本地 QA JSON']]) +
      select('env','环境',r.environment,[['test','测试'],['production','生产']]) + '</div>' +
      input('endpoint','连接地址',r.endpoint || '') + select('credential','凭据引用',r.credential_id || '',[['','无凭据'],...(ui.credentials || []).map(item => [item.credential_id,item.name + ' · ' + labels[item.environment]])]) +
      '<div class="tc-fields">' + select('auth-mode','身份注入',r.auth_mode,[['none','无'],['bearer','Bearer Token'],['api_key','API Key Header'],['basic','Basic / Neo4j 账号密码']]) +
      (mcp ? mcpConfig : '') + '</div><details class="tc-advanced"><summary>高级：说明、负责人、超时与兼容配置</summary>' + area('description','说明',r.description || '') + input('owner','负责人',r.owner || '') +
      '<div class="tc-fields">' + input('timeout','超时（秒）',r.timeout_seconds,'type="number" min="1" max="60"') + input('auth-header','API Key 头名称',r.auth_header || 'X-API-Key') + input('health-path','只读健康检查路径',r.health_path || '/health') + '</div>' + (!mcp ? otherConfig : '') + '</details>' +
      '<div class="tc-note">管理员点击“保存并发布”会一次完成保存和发布；历史回滚等低频操作已收进高级设置。MCP 外部主机必须加入 TOOL_CENTER_ALLOWED_HOSTS。</div>' +
      '<button type="submit" class="tc-btn primary" ' + (!manage() ? 'disabled' : '') + '>' + saveText + '</button></form>' + versionActions(r) +
      (r.version ? '<div class="tc-actions">' + btn('test','测试连接','data-tc-id="' + e(r.connection_id) + '"',false,!manage()) + '</div><div id="tc-test-result">' + (r.health ? json(r.health) : '') + '</div>' : '') + '</section>';
  }
  function credentialEditor(r) {
    r = r || {};
    const metadata = r.credential_id ? '<div class="tc-note">当前版本 v' + e(r.version) + ' · ' + e(r.source === 'environment' ? '服务器环境变量引用' : '服务器加密密文') + ' · ' + e(r.masked || '未配置') + ' · 更新于 ' + e(r.updated_at) + '</div>' : '';
    return '<section class="tc-panel tc-editor"><h2>' + (r.credential_id ? '查看 / 更新凭据（不会回显旧秘密）' : '新建凭据') + '</h2><form id="tc-editor">' + metadata +
      input('id','凭据 ID',r.credential_id || '',r.credential_id ? 'readonly' : 'required') + input('name','名称',r.name || '','required') +
      select('env','环境',r.environment || 'test',[['test','测试'],['production','生产']]) +
      input('secret-token','Token / API Key','','type="password" autocomplete="new-password"') + input('secret-username','账号（Basic / Neo4j）','','autocomplete="off"') +
      input('secret-password','密码','','type="password" autocomplete="new-password"') +
      '<div class="tc-note">只保存服务器加密密文；浏览器不读取旧秘密，也不写入 localStorage。更新后仍沿用同一个凭据 ID，未执行的确认申请须重新发起。</div>' +
      '<button class="tc-btn primary" type="submit" ' + (!admin() ? 'disabled' : '') + '>' + (r.credential_id ? '保存更新' : '保存加密凭据') + '</button></form></section>';
  }
  function credentialTable(items) {
    return table(['凭据名称','环境与存储','最近更新','操作'],items.map(item => '<tr data-tc-record="' + e(item.credential_id) + '" data-tc-credential-row="' + e(item.credential_id) + '" class="tc-clickable" tabindex="0" role="button"><td><div class="tc-resource"><span class="tc-resource-icon">' + icon('credentials') + '</span><div class="tc-resource-copy"><b title="' + e(item.name) + '">' + e(item.name) + '</b><small title="' + e(item.credential_id) + '">' + e(item.credential_id) + ' · v' + item.version + '</small></div></div></td><td>' +
      status(item.environment) + '<small>' + e(item.source === 'environment' ? '环境变量引用' : '加密存储') + ' · ' + e(item.masked) + '</small></td><td class="tc-date" title="' + e(item.updated_at) + '">' + e(dateLabel(item.updated_at)) + '</td><td>' +
      '<div class="tc-actions">' + btn('select','查看/更新','data-tc-id="' + e(item.credential_id) + '"') + btn('delete-credential','删除','data-tc-id="' + e(item.credential_id) + '"',false,!admin()) + '</div></td></tr>'));
  }
  function canDecide(call, approve) {
    if (!auth.principal || !call.status.startsWith('awaiting')) return false;
    if (!approve && call.actor_id === auth.principal.user_id) return true;
    return call.requires_approval ? has(['admin','approver']) && call.actor_id !== auth.principal.user_id : call.actor_id === auth.principal.user_id;
  }
  function callCard(call) {
    return '<article class="tc-call-card" data-tc-call-card="' + e(call.call_id) + '"><h3>' + e(call.tool_name || call.tool_id) + ' ' + status(call.status) + '</h3><div class="tc-note">' +
      e('申请人 ' + call.actor_id + ' · Agent ' + (call.app_id || '调试') + ' · v' + call.tool_version + ' · ' + labels[call.environment]) +
      '<br>' + e('调用 ' + call.call_id + ' · Trace ' + call.trace_id) + (call.status.startsWith('awaiting') ? '<br>有效期至 ' + e(new Date(call.expires_at * 1000).toLocaleString()) : '') + '</div>' +
      '<details><summary>核对调用参数</summary>' + json(call.parameters || {}) + '</details>' +
      (call.result ? json(call.result) : '') + (call.error ? '<div class="tc-note warning">' + e(call.error) + '</div>' : '') +
      (call.status === 'unknown' ? '<div class="tc-note warning">写操作可能已经生效，请先核对业务系统。不会自动重试。</div>' : '') +
      (call.requires_approval && call.actor_id === auth.principal?.user_id && call.status === 'awaiting_approval' ? '<div class="tc-note">申请人不能自行批准此高风险动作，请交由另一位审批者。</div>' : '') +
      '<div class="tc-actions">' + (canDecide(call,true) ? btn('approve',call.requires_approval ? '批准并执行' : '确认并执行','data-tc-call="' + e(call.call_id) + '"',true) : '') +
      (canDecide(call,false) ? btn('reject','拒绝 / 取消','data-tc-call="' + e(call.call_id) + '"') : '') + btn('refresh-call','刷新真实状态','data-tc-call="' + e(call.call_id) + '"') + '</div></article>';
  }
  function callsTable(items) {
    return table(['工具与调用人','Agent 与环境','状态与耗时','操作'],items.map(item => '<tr data-tc-record="' + e(item.call_id) + '"><td><div class="tc-resource"><span class="tc-resource-icon">' + icon('calls') + '</span><div class="tc-resource-copy"><b title="' + e(item.tool_name || item.tool_id) + '">' + e(item.tool_name || item.tool_id) + '</b><small title="' + e(item.call_id) + '">' + e(item.actor_id) + ' · ' + e(dateLabel(item.created_at)) +
      '</small></div></div></td><td><span class="tc-cell-title" title="' + e(item.app_id || '单工具调试') + '">' + e(item.app_id || '单工具调试') + '</span><small>' + e(labels[item.environment]) + ' · v' + item.tool_version + '</small></td><td>' + status(item.status) +
      '<small>' + e(item.duration_ms == null ? '尚未执行' : item.duration_ms + ' ms') + '</small></td><td>' + btn('select','详情','data-tc-id="' + e(item.call_id) + '"') + '</td></tr>'));
  }
  function importMarkup() {
    return '<section class="tc-panel tc-import"><h2>导入 / MCP 发现（先预览，再保存草稿）</h2><div class="tc-fields">' +
      select('import-format','格式','bundle',[['bundle','工具定义 JSON 包'],['openapi','OpenAPI 3.x JSON'],['mcp','MCP tools/list 发现']]) +
      select('import-connection','已有连接','',[['','请选择连接'],...(ui.connections || []).map(item => [item.connection_id,item.name])]) + '</div>' +
      '<label class="tc-field"><span>读取本地 JSON 文件</span><input id="tc-import-file" type="file" accept=".json,application/json"></label>' +
      area('import-document','JSON 文档（MCP 发现留空）',{}) + '<div class="tc-actions">' + btn('preview-import','校验并预览',null,true,!manage()) +
      btn('commit-import','保存预览为草稿',null,false,!manage() || !ui.preview) + btn('close-import','关闭') + '</div><div id="tc-import-preview">' +
      (ui.preview ? json(ui.preview) : '') + '</div></section>';
  }
  async function loadData() {
    const data = await Promise.all([api('/overview'),api('/tools'),api('/connections'),api('/credentials'),api('/calls?limit=200'),fetchJson('/api/apps')]);
    ui.overview=data[0]; ui.tools=data[1].tools; ui.connections=data[2].connections; ui.credentials=data[3].credentials; ui.calls=data[4].calls; ui.apps=data[5].apps;
  }
  async function loadSelection() {
    const list = ui[ui.tab] || [];
    if (ui.modalOpen && ui.selected && !ui.isNew) {
      if (['tools','connections'].includes(ui.tab)) ui.resource=(await api('/resources/' + ui.tab + '/' + encodeURIComponent(ui.selected))).resource;
      else ui.resource=list.find(item => (item.credential_id || item.call_id) === ui.selected);
    }
  }
  function editorMarkup() {
    if(ui.tab==='tools')return toolEditor(ui.resource);
    if(ui.tab==='connections')return connectionEditor(ui.resource);
    if(ui.tab==='credentials')return admin() ? credentialEditor(ui.resource) : '<section class="tc-panel"><h2>只读凭据元数据</h2><div class="tc-note">只有管理员可以创建或更新秘密。</div></section>';
    return ui.resource ? callCard(ui.resource) : '<section class="tc-panel">暂无调用记录。可先调试本地工单草稿。</section>';
  }
  function modalTitle() {return ui.isNew ? '新建' + tabs[ui.tab] : ui.resource?.name || ui.resource?.tool_name || tabs[ui.tab] + '详情';}
  function modalMarkup() {
    const title=modalTitle();
    return ui.modalOpen ? '<div class="tc-modal"><div class="tc-modal-backdrop" data-tc-action="close-editor"></div><section class="tc-modal-dialog" role="dialog" aria-modal="true" aria-label="' + e(title) + '"><div class="tc-modal-head"><span class="tc-modal-title">' + icon(ui.tab) + '<span>' + e(title) + '</span></span>' + btn('close-editor','关闭',null,false) + '</div><div class="tc-modal-content">' + editorMarkup() + '</div></section></div>' : '';
  }
  function updateModal() {
    const root=document.getElementById('subview');if(!root)return;
    let modal=root.querySelector('.tc-modal');
    if(!ui.modalOpen){modal?.remove();return;}
    if(!modal){(root.querySelector('.tc-page') || root).insertAdjacentHTML('beforeend',modalMarkup());modal=root.querySelector('.tc-modal');}
    else {
      const dialog=modal.querySelector('.tc-modal-dialog');const scrollTop=dialog.scrollTop;
      const open=[...modal.querySelectorAll('.tc-advanced')].map(section=>section.open);
      modal.querySelector('.tc-modal-content').innerHTML=editorMarkup();
      modal.querySelector('.tc-modal-title>span').textContent=modalTitle();
      dialog.setAttribute('aria-label',modalTitle());
      modal.querySelectorAll('.tc-advanced').forEach((section,index)=>{section.open=!!open[index]});
      dialog.scrollTop=scrollTop;
    }
    bind(modal);
  }
  async function openEditor(id) {
    ui.selected=id;ui.isNew=false;ui.modalOpen=true;ui.resource=null;ui.lastCall=null;
    await loadSelection();updateModal();
  }
  function summaryMarkup() {
    const overview=ui.overview;
    const summaries=[['tools','已登记工具',overview.tool_count],['connections','数据连接',overview.connection_count],['pending','待确认 / 审批',overview.pending_count],['calls','最近调用',overview.call_count]];
    return summaries.map(([id,label,count]) => '<article><span class="tc-stat-icon ' + id + '">' + icon(id) + '</span><div><span>' + label + '</span><b>' + count + '</b></div></article>').join('');
  }
  async function render() {
    await ready;
    if (!auth.principal) return subviewShell('tools',loginMarkup());
    await loadData();await loadSelection();
    const actions = ui.tab === 'calls' ? btn('audit','查看治理审计',null,false,!manage()) : btn('new','新建' + tabs[ui.tab],null,true,ui.tab === 'credentials' ? !admin() : !manage());
    const more=manage() ? '<details class="tc-more"><summary>更多操作 <span aria-hidden="true">⋯</span></summary><div class="tc-more-menu">' + btn('export','导出定义（不含秘密）') + btn('import','导入 / MCP 发现') + (ui.tab==='tools' ? btn('template','从内置模板新建') : '') + '</div></details>' : '';
    const visibleWarnings=(ui.overview.warnings || []).filter(item => !String(item).startsWith('开发模式：'));
    return '<div class="tc-page">' + subviewShell('tools','<div class="tc-summary">' + summaryMarkup() + '</div>' +
      visibleWarnings.map(text => '<div class="tc-note warning">' + e(text) + '</div>').join('') +
      '<section class="tc-panel tc-list-panel"><div class="tc-list-head">' + tabsMarkup() + '<div class="tc-actions">' + more + actions + '</div></div>' + filtersMarkup() +
      (ui.importOpen ? importMarkup() : '') + '<div id="tc-records">' + recordsMarkup() + '</div><div id="tc-audit"></div></section>' + modalMarkup(),sessionMarkup()) + '</div>';
  }
  async function refresh() {
    const root=document.getElementById('subview');
    if(!auth.principal || !root?.querySelector('#tc-records'))return renderSubview('tools');
    await loadData();await loadSelection();
    root.querySelector('.tc-summary').innerHTML=summaryMarkup();
    root.querySelectorAll('[data-tc-count]').forEach(node=>{node.textContent=(ui[node.dataset.tcCount] || []).length});
    updateRecords();updateModal();
  }
  function resetSelection() {ui.selected=null;ui.resource=null;ui.isNew=false;ui.modalOpen=false;ui.page=1;ui.lastCall=null;ui.invokeKey=null;ui.invokeBody=null;}
  async function save() {
    let payload;
    if (ui.tab === 'credentials') {
      const secret={}; if(value('secret-token'))secret.token=value('secret-token'); if(value('secret-username'))secret.username=value('secret-username'); if(value('secret-password'))secret.password=value('secret-password');
      payload={credential_id:value('id'),name:value('name'),environment:value('env'),secret};
    } else if (ui.tab === 'connections') {
      payload={connection_id:value('id'),name:value('name'),description:value('description'),owner:value('owner'),type:value('type'),environment:value('env'),endpoint:value('endpoint'),
        credential_id:value('credential') || null,auth_mode:value('auth-mode'),auth_header:value('auth-header'),health_path:value('health-path'),timeout_seconds:Number(value('timeout')),config:readConnectionConfig(),enabled:true,
        base_version:ui.isNew ? null : ui.resource?.version};
    } else {
      payload={tool_id:value('id'),name:value('name'),description:value('description'),owner:value('owner'),kind:value('kind'),handler:value('handler') || null,
        connection_id:value('connection') || null,environment:value('env'),risk:value('risk'),method:value('method'),path:value('path'),required_permission:value('permission'),
        allowed_roles:value('roles').split(',').map(item=>item.trim()).filter(Boolean),timeout_seconds:Number(value('timeout')),rate_limit_per_minute:Number(value('rate')),
        input_schema:parse('input-schema'),output_schema:parse('output-schema'),config:parse('config'),enabled:true,base_version:ui.isNew ? null : ui.resource?.version};
    }
    const result=await api('/' + ui.tab,payload); ui.resource=result.tool || result.connection || result.credential;
    ui.selected=ui.resource.tool_id || ui.resource.connection_id || ui.resource.credential_id;ui.isNew=false;
    const autoPublish = admin() && ['tools','connections'].includes(ui.tab);
    if (autoPublish) await api('/resources/' + ui.tab + '/' + encodeURIComponent(ui.selected) + '/publish',{version:ui.resource.version,note:'保存并发布'});
    message(ui.tab === 'credentials' ? '加密凭据已保存；旧值未回显' : autoPublish ? '已保存并发布' : '草稿已保存，等待管理员发布'); await refresh();
  }
  async function mutateBinding(remove) {
    const id=value('bind-app'); if(!id)throw new Error('请选择应用');
    const result=await fetchJson('/api/apps/' + encodeURIComponent(id)); const app=result.app;
    if(!confirm((remove ? '移除' : '添加') + '工具授权并发布应用“' + app.name + '”。\n这会发布其当前草稿的全部配置，确认继续？'))return;
    const keys=['app_id','name','description','category','type','icon','enabled','tags','workflow_id','workflow_version','workflow_bindings','data_sources','tools','runtime','permissions','channels'];
    const payload={}; keys.forEach(key=>payload[key]=app[key]); payload.base_version=app.version;
    payload.tools=(app.tools || []).filter(item=>canonical(item.tool_id)!==ui.resource.tool_id);
    if(!remove)payload.tools.push({tool_id:ui.resource.tool_id,name:ui.resource.name,enabled:true,requires_confirmation:['confirm','approval','device_control'].includes(ui.resource.risk),config:{}});
    const saved=await fetchJson('/api/apps/' + encodeURIComponent(id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    await fetchJson('/api/apps/' + encodeURIComponent(id) + '/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:saved.app.version,note:'工具中心授权更新'})});
    message('应用授权已更新并发布');await refresh();
  }
  async function invocation(debug) {
    const parameters=parse('parameters'); const app=value('invoke-app') || null;
    const signature=JSON.stringify([ui.resource.tool_id,parameters,app,debug]);
    if(ui.invokeBody !== signature){ui.invokeKey=crypto.randomUUID();ui.invokeBody=signature;}
    const result=await api('/tools/' + encodeURIComponent(ui.resource.tool_id) + '/invoke',{parameters,app_id:debug ? null : app,idempotency_key:ui.invokeKey,debug});
    ui.lastCall=result.call; const target=document.getElementById('tc-invoke-result'); if(target)target.innerHTML=callCard(result.call); bindCards(target);
  }
  async function decide(cid, approve) {
    const result=await api('/calls/' + encodeURIComponent(cid)); const call=result.call;
    if(approve && !confirm('将执行：' + call.tool_name + '\n申请人：' + call.actor_id + '\nAgent：' + (call.app_id || '调试') +
      '\n环境：' + labels[call.environment] + '\n参数：\n' + JSON.stringify(call.parameters,null,2) + '\n\n确认/批准后会真实调用业务系统，是否继续？'))return;
    const data=await api('/calls/' + encodeURIComponent(cid) + '/decision',{decision:approve ? 'approve' : 'reject',note:approve ? '用户在页面核对后明确确认' : '用户拒绝/取消'});
    updateCards(data.call); if(ui.selected===cid)ui.resource=data.call; if(ui.lastCall?.call_id===cid)ui.lastCall=data.call;
    message(labels[data.call.status] || data.call.status, ['error','unknown'].includes(data.call.status));
  }
  function updateCards(call) {
    document.querySelectorAll('[data-tc-call-card]').forEach(card=>{
      if(card.dataset.tcCallCard===call.call_id){card.outerHTML=callCard(call);}
    }); bindCards(document);
  }
  function bindCards(root) {
    if(!root)return;
    root.querySelectorAll('[data-tc-call]').forEach(button=>{
      if(button.dataset.tcCardBound)return;button.dataset.tcCardBound='1';
      button.addEventListener('click',async event=>{
        event.stopPropagation();event.preventDefault();button.disabled=true;
        try{
          if(button.dataset.tcAction==='refresh-call'){const result=await api('/calls/' + encodeURIComponent(button.dataset.tcCall));updateCards(result.call);}
          else await decide(button.dataset.tcCall,button.dataset.tcAction==='approve');
        }catch(error){message(error.message,true)}finally{button.disabled=false}
      });
    });
  }
  async function action(button) {
    const name=button.dataset.tcAction;
    if(name==='refresh')return refresh();
    if(name==='select')return openEditor(button.dataset.tcId);
    if(name==='new'){const page=ui.page;resetSelection();ui.page=page;ui.isNew=true;ui.modalOpen=true;return updateModal();}
    if(name==='close-editor'){ui.modalOpen=false;return updateModal();}
    if(name==='page-prev'){ui.page=Math.max(1,ui.page-1);return updateRecords();}
    if(name==='page-next'){ui.page+=1;return updateRecords();}
    if(name==='login'||name==='local-login'){
      const result=await rawJson('/api/tool-center/session/' + (name==='login'?'login':'local'),{method:'POST',headers:{'Content-Type':'application/json','X-Tool-Center-Login':'1'},body:JSON.stringify(name==='login'?{token:value('login-token')}:{})});
      setAuth(result);ready=Promise.resolve();return refresh();
    }
    if(name==='logout'){await api('/session/logout',{});setAuth({principal:null,csrf_token:null});resetSelection();return refresh();}
    if(name==='delete-credential'){
      const cid=button.dataset.tcId; const item=(ui.credentials || []).find(entry=>entry.credential_id===cid);
      if(!item)throw new Error('凭据不存在或列表已刷新');
      if(!confirm('确认删除凭据“' + item.name + '”？\n仍被连接当前或历史版本引用的凭据无法删除。'))return;
      await api('/credentials/' + encodeURIComponent(cid),undefined,'DELETE');
      if(ui.selected===cid)resetSelection();
      message('凭据已删除');return refresh();
    }
    if(name==='publish'){await api('/resources/' + ui.tab + '/' + encodeURIComponent(ui.selected) + '/publish',{version:ui.resource.version,note:'工具与数据中心发布'});message('发布成功');return refresh();}
    if(name==='toggle'){await api('/resources/' + ui.tab + '/' + encodeURIComponent(ui.selected) + '/enabled',{enabled:!ui.resource.enabled});return refresh();}
    if(name==='rollback'){
      const version=Number(value('history'));if(!confirm('回滚至 v' + version + ' 并发布为新版本？'))return;
      await api('/resources/' + ui.tab + '/' + encodeURIComponent(ui.selected) + '/rollback',{version,note:'页面回滚'});message('已生成并发布回滚版本');return refresh();
    }
    if(name==='compare'){const result=await api('/resources/' + ui.tab + '/' + encodeURIComponent(ui.selected) + '/compare?from_version=' + value('history') + '&to_version=' + ui.resource.version);document.getElementById('tc-diff').innerHTML=json(result);return;}
    if(name==='test'){
      const result=await api('/connections/' + encodeURIComponent(button.dataset.tcId) + '/test',{});message(result.health.available ? '真实连接测试通过' : result.health.error, !result.health.available);await refresh();
      const target=document.getElementById('tc-test-result');if(target && ui.selected===button.dataset.tcId)target.innerHTML=json(result.health);return;
    }
    if(name==='debug'||name==='invoke')return invocation(name==='debug');
    if(name==='new-invocation'){if(ui.lastCall?.status.startsWith('awaiting'))throw new Error('请先确认、拒绝或取消当前申请');ui.invokeKey=null;ui.invokeBody=null;ui.lastCall=null;document.getElementById('tc-invoke-result').innerHTML='';return;}
    if(name==='bind'||name==='unbind')return mutateBinding(name==='unbind');
    if(name==='audit'){const result=await api('/audit?limit=100');document.getElementById('tc-audit').innerHTML='<h3>最近治理审计</h3>' + json(result.events);return;}
    if(name==='import'){ui.importOpen=true;return renderSubview('tools');}
    if(name==='close-import'){ui.importOpen=false;ui.preview=null;return renderSubview('tools');}
    if(name==='preview-import'){
      ui.preview=await api('/imports/preview',{format:value('import-format'),document:parse('import-document'),connection_id:value('import-connection') || null});
      document.getElementById('tc-import-preview').innerHTML=json(ui.preview);document.querySelector('[data-tc-action="commit-import"]').disabled=!manage();return;
    }
    if(name==='commit-import'){if(!ui.preview)throw new Error('请先预览');await api('/imports',{format:'bundle',document:{schema_version:1,tools:ui.preview.tools,connections:ui.preview.connections}});ui.importOpen=false;ui.preview=null;message('导入草稿成功；请逐项发布并授权');resetSelection();return renderSubview('tools');}
    if(name==='export'){
      const documentData=await api('/export');const blob=new Blob([JSON.stringify(documentData,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);
      const link=document.createElement('a');link.href=url;link.download='tool-center-' + auth.workspace + '.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);return;
    }
    if(name==='template'){
      const data=await api('/templates');const id=prompt('输入模板 ID：\n' + data.templates.map(item=>item.template_id + '（' + item.name + '）').join('\n'),'get_device_status');
      const template=data.templates.find(item=>item.template_id===id);if(!template)return;
      resetSelection();ui.tab='tools';ui.isNew=true;ui.modalOpen=true;ui.resource=Object.assign({},template.definition,{tool_id:'',environment:'test',connection_id:null,owner:auth.principal.user_id});return renderSubview('tools');
    }
  }
  function bind(root=document.getElementById('subview')) {
    if(!root)return;
    root.querySelectorAll('[data-tc-tab]').forEach(button=>button.onclick=()=>{ui.tab=button.dataset.tcTab;ui.status='';resetSelection();renderSubview('tools').catch(error=>message(error.message,true))});
    root.querySelectorAll('[data-tc-action]:not([data-tc-call])').forEach(button=>button.onclick=async event=>{
      event.preventDefault();button.disabled=true;try{await action(button)}catch(error){message(error.message,true)}finally{button.disabled=false}
    });
    root.querySelectorAll('[data-tc-credential-row]').forEach(row=>{
      const open=()=>openEditor(row.dataset.tcCredentialRow).catch(error=>message(error.message,true));
      row.addEventListener('click',event=>{if(event.target.closest('button,a,input,select,textarea'))return;open();});
      row.addEventListener('keydown',event=>{if(event.key==='Enter' || event.key===' '){event.preventDefault();open();}});
    });
    root.querySelector('#tc-editor')?.addEventListener('submit',async event=>{
      event.preventDefault();const button=event.submitter;button.disabled=true;try{await save()}catch(error){message(error.message,true)}finally{button.disabled=false}
    });
    root.querySelector('#tc-filter')?.addEventListener('input',event=>{ui.filter=event.target.value;ui.page=1;updateRecords()});
    root.querySelector('#tc-filter-env')?.addEventListener('change',event=>{ui.environment=event.target.value;ui.page=1;updateRecords()});
    root.querySelector('#tc-filter-status')?.addEventListener('change',event=>{ui.status=event.target.value;ui.page=1;updateRecords()});
    root.querySelector('#tc-workspace')?.addEventListener('change',event=>{auth.workspace=event.target.value;setAuth({principal:auth.principal,csrf_token:auth.csrf});resetSelection();renderSubview('tools')});
    root.querySelector('#tc-import-file')?.addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;if(file.size>1000000){message('文件超过 1 MB',true);return;}document.getElementById('tc-import-document').value=await file.text();ui.preview=null;root.querySelector('[data-tc-action="commit-import"]').disabled=true;});
    root.querySelector('#tc-import-document')?.addEventListener('input',()=>{ui.preview=null;root.querySelector('[data-tc-action="commit-import"]').disabled=true;});
    root.querySelector('#tc-kind')?.addEventListener('change',()=>{ui.resource=Object.assign({},ui.resource || {},{tool_id:value('id'),name:value('name'),description:value('description'),kind:value('kind')});root.querySelector('#tc-risk').value=value('kind')==='builtin'?'draft':value('kind')==='mcp'?'confirm':'read_only';root.querySelector('#tc-handler').value=value('kind')==='builtin'?'draft_work_order':'';});
    root.querySelector('#tc-type')?.addEventListener('change',async()=>{
      try{
        let config=ui.resource?.config || {};
        if(document.getElementById('tc-config'))config=parse('config');
        ui.resource=Object.assign({},ui.resource || {},{connection_id:value('id'),name:value('name'),description:value('description'),owner:value('owner') || ui.resource?.owner || '',type:value('type'),environment:value('env'),endpoint:value('endpoint'),credential_id:value('credential') || null,auth_mode:value('auth-mode'),auth_header:value('auth-header') || ui.resource?.auth_header || 'X-API-Key',health_path:value('health-path') || ui.resource?.health_path || '/health',timeout_seconds:Number(value('timeout')) || ui.resource?.timeout_seconds || 8,config});
        updateModal();
      }catch(error){message(error.message,true)}
    });
    root.querySelector('#tc-connection')?.addEventListener('change',()=>{const conn=(ui.connections || []).find(item=>item.connection_id===value('connection'));if(conn)root.querySelector('#tc-env').value=conn.environment;});
    bindCards(root);
  }
  viewMeta.tools.desc='统一管理 Agent 工具与数据资源，追踪每一次真实调用。';
  renderTools=render;
  const previousSubview=renderSubview;
  renderSubview=async function(view){await previousSubview(view);if(view==='tools')bind();if(view==='apps')await enhanceApplication();};
  const previousResult=renderResult;
  renderResult=function(result,bubble){
    previousResult(result,bubble);
    if(result.tool_calls?.length){
      const container=document.createElement('div');container.className='tc-call-cards';container.innerHTML=result.tool_calls.map(callCard).join('');bubble.appendChild(container);bindCards(container);
    }
  };
  appToolOptions=function(app){
    const current=new Map((app.tools || []).map(item=>[canonical(item.tool_id),item]));
    const catalog=new Map((state.appToolCatalog || []).map(item=>[canonical(item.tool_id || item.name),item]));
    current.forEach((item,id)=>{if(!catalog.has(id))catalog.set(id,{tool_id:id,name:item.name,risk:'unknown'})});
    return [...catalog.entries()].map(([id,item])=>{
      const bound=current.get(id);const available=item.enabled!==false && !!item.published_version;
      return '<label class="app-check"><input type="checkbox" data-app-tool="' + e(id) + '" ' + (bound && bound.enabled!==false?'checked':'') + ' ' + (!available?'disabled':'') +
        '><span><b>' + e(item.name || id) + '</b><small>' + e(labels[item.risk] || item.risk) + ' · ' + e(available?'已发布能力':'未发布/已停用') + '</small></span></label>';
    }).join('');
  };
  const previousAppPayload=appCollectPayload;
  appCollectPayload=function(){
    const payload=previousAppPayload();
    payload.runtime.environment=document.getElementById('app-tool-environment')?.value || payload.runtime.environment || 'production';
    for(const source of payload.data_sources){
      const picker=[...document.querySelectorAll('[data-app-connection-picker]')].find(item=>item.dataset.appConnectionSource===source.source_type);
      if(picker){
        const select=picker.tagName==='SELECT' ? picker : picker.querySelector('select');
        const ids=select ? [...select.selectedOptions].map(option=>option.value) : [...picker.querySelectorAll('input[data-app-connection]')].filter(input=>input.checked).map(input=>input.dataset.appConnection);
        source.config={...source.config,connection_ids:ids};
        if(source.source_type==='sql'){
          const scopeField=picker.parentElement?.querySelector('textarea[data-app-sql-scope]') || picker.querySelector('textarea[data-app-sql-scope]');
          if(scopeField){
            try{
              const value=scopeField.value.trim();
              const scope=value ? JSON.parse(value) : {};
              if(!scope || typeof scope!=='object' || Array.isArray(scope))throw new Error('对象');
              source.config={...source.config,data_scope:scope};
            }catch(error){throw new Error('应用结构化数据范围必须是合法 JSON 对象');}
          }
        }
        continue;
      }
      // 兼容工具中心脚本未加载或旧页面缓存时的旧版连接选择器。
      const checks=[...document.querySelectorAll('[data-app-connection-source]')].filter(input=>input.dataset.appConnectionSource===source.source_type);
      if(checks.length)source.config={...source.config,connection_ids:checks.filter(input=>input.checked).map(input=>input.dataset.appConnection)};
    }
    for(const tool of payload.tools){
      const definition=(state.appToolCatalog || []).find(item=>canonical(item.tool_id || item.name)===canonical(tool.tool_id));
      tool.tool_id=canonical(tool.tool_id);
      if(definition?.risk==='draft')tool.requires_confirmation=false;
    }
    return payload;
  };
  function applicationConnectionStatus(connection){
    const health=connection.health?.status || 'unknown';
    return `${connection.published_version ? '已发布' : '未发布'} · ${labels[health] || '未测试/结果不确定'}`;
  }
  function applicationSqlScopeEditor(binding){
    const scope=binding?.config?.data_scope;
    const value=scope && typeof scope==='object' ? JSON.stringify(scope,null,2) : '{\n  "datasets": {}\n}';
    return `<label class="tc-sql-scope"><span>应用结构化数据范围（可选 JSON）</span><textarea data-app-sql-scope rows="4" placeholder="{\n  &quot;datasets&quot;: {\n    &quot;设备报警&quot;: { &quot;fields&quot;: [&quot;device_id&quot;, &quot;occurred_at&quot;] }\n  },\n  &quot;max_rows&quot;: 50\n}">${e(value)}</textarea><small>只能缩小 SQL 连接的已发布范围；生产环境仍要求数据集和字段白名单。</small></label>`;
  }
  function applicationConnectionPicker(type, connections, binding, environment){
    const enabledConnections=connections.filter(item=>item.enabled && item.published_version && item.environment===environment);
    const configuredIds=Array.isArray(binding?.config?.connection_ids) ? binding.config.connection_ids : null;
    const selectedIds=configuredIds ? enabledConnections.filter(item=>configuredIds.includes(item.connection_id)).map(item=>item.connection_id) : enabledConnections.map(item=>item.connection_id);
    const pickerAttribute=`data-app-connection-picker data-app-connection-source="${e(type)}"`;
    if(!enabledConnections.length){
      return `<div class="tc-connection-summary is-empty" ${pickerAttribute}><span class="tc-connection-summary-label">连接实例</span><strong>未配置当前环境的已发布连接</strong><small>请先在“工具与数据 → 数据连接”中保存并发布连接。</small></div>`;
    }
    if(enabledConnections.length===1){
      const connection=enabledConnections[0];
      const selected=selectedIds.includes(connection.connection_id);
      return `<div class="tc-connection-summary ${selected?'':'is-warning'}" ${pickerAttribute}><span class="tc-connection-summary-label">实际连接</span><div class="tc-connection-summary-main"><strong>${e(connection.name)} · ${e(labels[connection.environment] || connection.environment)}</strong><small>${e(applicationConnectionStatus(connection))}</small></div><input type="checkbox" hidden data-app-connection="${e(connection.connection_id)}" ${selected?'checked':''}></div>${type==='sql'?applicationSqlScopeEditor(binding):''}`;
    }
    const options=enabledConnections.map(connection=>`<option value="${e(connection.connection_id)}" ${selectedIds.includes(connection.connection_id)?'selected':''}>${e(connection.name)} · ${e(labels[connection.environment] || connection.environment)} · ${e(applicationConnectionStatus(connection))}</option>`).join('');
    return `<div class="tc-connection-picker" ${pickerAttribute}><div class="tc-connection-picker-head"><span>连接实例</span><small>当前环境可用，可多选</small></div><select multiple size="${Math.min(4,Math.max(2,enabledConnections.length))}" aria-label="${e(type)} 连接实例">${options}</select><small class="tc-connection-picker-note">未发布、停用或其他环境的连接不会参与运行。</small></div>${type==='sql'?applicationSqlScopeEditor(binding):''}`;
  }
  async function enhanceApplication(){
    await ready;if(!auth.principal || !state.app)return;
    const data=await api('/connections');const environment=state.app.runtime?.environment || 'production';
    const root=document.getElementById('subview');
    for(const checkbox of root.querySelectorAll('[data-app-source]')){
      const type=checkbox.dataset.appSource;const binding=(state.app.data_sources || []).find(item=>item.source_type===type);
      checkbox.checked=!!binding && binding.enabled!==false;
      const connections=data.connections.filter(item=>({ragflow:'document',neo4j:'graph',business_api:'api',sql:'sql',qa:'qa',mcp:'mcp'}[item.type]===type));
      const label=checkbox.closest('.app-check');const wrapper=document.createElement('div');
      wrapper.className='app-source-card';
      label.parentNode.insertBefore(wrapper,label);wrapper.appendChild(label);
      wrapper.insertAdjacentHTML('beforeend',applicationConnectionPicker(type,connections,binding,environment));
      const syncSourceState=()=>{const enabled=checkbox.checked;wrapper.classList.toggle('is-disabled',!enabled);wrapper.querySelectorAll('select,input[data-app-connection],textarea[data-app-sql-scope]').forEach(input=>{input.disabled=!enabled;});};
      checkbox.addEventListener('change',syncSourceState);syncSourceState();
    }
    root.querySelectorAll('textarea[data-app-sql-scope]').forEach(field=>field.addEventListener('input',()=>{state.appDirty=true;}));
    const section=[...root.querySelectorAll('.app-section')].find(item=>item.querySelector('h3')?.textContent==='运行策略');
    if(section){
      section.insertAdjacentHTML('afterbegin','<label class="tc-field"><span>工具 / 数据运行环境（保存并发布后生效）</span><select id="app-tool-environment"><option value="production" ' +
        (environment==='production'?'selected':'') + '>生产</option><option value="test" ' + (environment==='test'?'selected':'') + '>测试</option></select></label>');
      root.querySelector('#app-tool-environment').onchange=event=>{
        root.querySelectorAll('[data-app-connection-env]').forEach(input=>{input.disabled=input.dataset.appConnectionAvailable!=='true' || input.dataset.appConnectionEnv!==event.target.value;if(input.disabled)input.checked=false;});
        state.appDirty=true;
      };
    }
    root.querySelectorAll('[data-app-action]').forEach(button=>{
      if(['save','create'].includes(button.dataset.appAction) && !manage())button.disabled=true;
      if(['publish','rollback'].includes(button.dataset.appAction) && !admin())button.disabled=true;
    });
    root.querySelectorAll('.app-inline-note').forEach(note=>{
      if(note.textContent.includes('谁能运行'))note.textContent='运行/编辑角色和已发布的工具、数据连接授权已由后端校验；发布仅限服务器管理员。部门目录与企业 SSO 仍需正式对接。';
    });
  }
  window.ToolCenter={ready:()=>ready,auth,refresh};
})();

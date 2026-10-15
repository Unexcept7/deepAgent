(function () {
  'use strict';

  viewMeta.models = {
    title: '模型中心',
    desc: '维护多个模型档案，并按任务难度和 Agent 阶段自动选择快速、均衡、推理或多模态模型。'
  };

  const oldRender = renderSubview;
  const e = escapeHtml;
  let registry = null;
  let selectedId = null;
  let creatingNew = false;
  let profileDirty = false;
  let message = '';
  let messageKind = '';

  const tierLabels = { low: '低成本', medium: '均衡', high: '高推理' };
  const complexityLabels = { simple: '简单', normal: '常规', complex: '复杂' };
  const capabilityLabels = {
    json: 'JSON 输出',
    tool_call: '工具调用',
    vision: '图片理解',
    long_context: '长上下文'
  };

  function selected(value, current) {
    return String(value ?? '') === String(current ?? '') ? ' selected' : '';
  }

  function checked(value) {
    return value ? ' checked' : '';
  }

  function profileById(id) {
    return (registry?.profiles || []).find(item => item.profile_id === id) || null;
  }

  function stageById(id) {
    return (registry?.stages || []).find(item => item.stage === id) || null;
  }

  function routingByStage(id) {
    return (registry?.routing || []).find(item => item.stage === id) || null;
  }

  function statusClass(profile) {
    if (profile?.ready) return 'ready';
    if (profile?.enabled === false) return 'disabled';
    return 'pending';
  }

  function statusText(profile) {
    if (profile?.ready) return '已配置';
    if (profile?.enabled === false) return '已停用';
    return '待配置';
  }

  function capabilityText(values) {
    return (values || []).map(item => capabilityLabels[item] || item).join('、') || '未声明';
  }

  function profileOptions(current) {
    return (registry?.profiles || []).map(profile => {
      const state = profile.ready ? '可用' : profile.enabled === false ? '停用' : '待配置';
      return `<option value="${e(profile.profile_id)}"${selected(profile.profile_id, current)}>${e(profile.name || profile.profile_id)} · ${e(profile.model || '未设置')} · ${state}</option>`;
    }).join('');
  }

  function profileCards() {
    const items = registry?.profiles || [];
    if (!items.length) return '<div class="mc-empty">暂无模型档案，请创建一个模型档案。</div>';
    return items.map(profile => `
      <button type="button" class="mc-profile-item${profile.profile_id === selectedId ? ' selected' : ''}" data-mc-profile="${e(profile.profile_id)}">
        <span class="mc-profile-title"><strong>${e(profile.name || profile.profile_id)}</strong><i class="mc-status ${statusClass(profile)}">${statusText(profile)}</i></span>
        <span class="mc-profile-model">${e(profile.model || '未填写模型名')}</span>
        <span class="mc-profile-meta">${e(profile.provider || 'OpenAI 兼容')} · ${e(tierLabels[profile.cost_tier] || profile.cost_tier || '均衡')}</span>
      </button>`).join('');
  }

  function profileEditor() {
    const profile = profileById(selectedId) || {
      profile_id: '',
      name: '',
      provider: 'OpenAI-compatible',
      model: '',
      base_url: '',
      base_url_env: 'DASHSCOPE_BASE_URL',
      api_key_env: 'DASHSCOPE_API_KEY',
      capabilities: ['json'],
      cost_tier: 'medium',
      temperature: 0,
      max_tokens: 8192,
      timeout_seconds: 60,
      max_retries: 1,
      enable_thinking: null,
      enabled: true,
      description: ''
    };
    const isNew = !profile.profile_id;
    const thinking = profile.enable_thinking === null || profile.enable_thinking === undefined
      ? '' : String(Boolean(profile.enable_thinking));
    const issues = Array.isArray(profile.issues) ? profile.issues : [];
    const diagnostics = isNew
      ? '<div class="mc-diagnostics pending"><strong>保存前检查</strong><span>填写模型信息并保存后，这里会显示具体的可用性检查结果。</span></div>'
      : issues.length
        ? `<div class="mc-diagnostics error"><strong>当前档案有 ${issues.length} 个问题</strong><ul>${issues.map(item => `<li><b>${e(item.field || '配置')}</b>${e(item.message || '')}</li>`).join('')}</ul></div>`
        : '<div class="mc-diagnostics ok"><strong>配置检查通过</strong><span>模型档案已启用，Base URL、密钥引用和基础能力均已满足。</span></div>';
    return `<section class="mc-card mc-editor">
      <div class="mc-section-head">
        <div><span class="mc-kicker">${isNew ? '新建模型档案' : '当前模型档案'}</span><h2>${e(profile.name || '填写模型连接信息')}</h2><p>密钥只保存环境变量名，真实密钥不会进入浏览器或配置 JSON。</p></div>
        <span class="mc-status ${statusClass(profile)}">${isNew ? '待保存' : statusText(profile)}</span>
      </div>
      ${diagnostics}
      <form id="mc-profile-form" class="mc-form">
        <div class="mc-form-grid">
          <label>模型 ID<input id="mc-profile_id" value="${e(profile.profile_id)}" maxlength="80" ${isNew ? '' : 'disabled'} placeholder="例如 qwen-fast"></label>
          <label>显示名称<input id="mc-name" value="${e(profile.name)}" maxlength="120" placeholder="例如 快速模型"></label>
          <label>提供方<input id="mc-provider" value="${e(profile.provider)}" maxlength="80" placeholder="DashScope / OpenAI-compatible"></label>
          <label>模型名称<input id="mc-model" value="${e(profile.model)}" maxlength="160" placeholder="例如 qwen-plus"></label>
          <label>直接 Base URL（可选）<input id="mc-base_url" value="${e(profile.base_url)}" maxlength="500" placeholder="留空则读取下面的环境变量"></label>
          <label>Base URL 环境变量<input id="mc-base_url_env" value="${e(profile.base_url_env)}" maxlength="120" placeholder="例如 DASHSCOPE_BASE_URL"></label>
          <label>API Key 环境变量<input id="mc-api_key_env" value="${e(profile.api_key_env)}" maxlength="120" placeholder="例如 DASHSCOPE_API_KEY"></label>
          <label>成本档位<select id="mc-cost_tier"><option value="low"${selected(profile.cost_tier, 'low')}>低成本 / 快速</option><option value="medium"${selected(profile.cost_tier, 'medium')}>均衡</option><option value="high"${selected(profile.cost_tier, 'high')}>高推理 / 复杂任务</option></select></label>
          <label>能力（逗号分隔）<input id="mc-capabilities" value="${e((profile.capabilities || []).join(', '))}" maxlength="300" placeholder="json, tool_call, vision"></label>
          <label>温度<input id="mc-temperature" type="number" min="0" max="2" step="0.1" value="${e(profile.temperature ?? 0)}"></label>
          <label>最大输出 Token<input id="mc-max_tokens" type="number" min="128" max="32768" step="1" value="${e(profile.max_tokens ?? 8192)}"></label>
          <label>请求超时（秒）<input id="mc-timeout_seconds" type="number" min="1" max="3600" step="1" value="${e(profile.timeout_seconds ?? 60)}"></label>
          <label>最大重试次数<input id="mc-max_retries" type="number" min="0" max="5" step="1" value="${e(profile.max_retries ?? 1)}"></label>
          <label>思考模式<select id="mc-enable_thinking"><option value=""${selected(thinking, '')}>自动 / 不额外指定</option><option value="true"${selected(thinking, 'true')}>开启</option><option value="false"${selected(thinking, 'false')}>关闭</option></select></label>
          <label>档案说明<input id="mc-description" value="${e(profile.description || '')}" maxlength="1000" placeholder="说明适用阶段和成本特点"></label>
        </div>
        <label class="mc-check"><input id="mc-enabled" type="checkbox"${checked(profile.enabled !== false)}>启用此模型档案</label>
        <div class="mc-help"><strong>配置建议：</strong>简单任务绑定低成本模型，复杂问题和证据冲突绑定推理模型；涉及图片的请求必须选择包含“图片理解”能力的档案。当前 Qwen 档案始终作为安全回退。</div>
      </form>
      <div class="mc-actions"><button type="button" class="mc-btn primary" data-mc="save-profile">保存模型档案</button><button type="button" class="mc-btn" data-mc="test-profile"${isNew ? ' disabled' : ''}>测试连接</button></div>
      <label class="mc-test-label">测试问题<textarea id="mc-test-prompt" rows="3">请用一句话说明你适合处理什么任务。</textarea></label>
      <div id="mc-test-result" class="mc-result">测试连接只测试服务端已保存的模型档案；如果刚修改了表单，请先点击“保存模型档案”。</div>
    </section>`;
  }

  function defaultRule(stage) {
    return {
      stage: stage.stage,
      name: stage.name,
      simple_profile: 'balanced',
      normal_profile: 'balanced',
      complex_profile: 'balanced',
      fallback_profile: 'balanced',
      required_capabilities: stage.required_capabilities || [],
      enabled: true
    };
  }

  function routingEditor() {
    const stages = registry?.stages || [];
    if (!stages.length) return '<div class="mc-empty">暂无阶段目录。</div>';
    const rows = stages.map(stage => {
      const rule = routingByStage(stage.stage) || defaultRule(stage);
      return `<tr data-mc-stage="${e(stage.stage)}">
        <td><strong>${e(stage.name)}</strong><small>${e(stage.description || '')}</small><code>${e(stage.stage)}</code></td>
        <td><select data-mc-route="simple">${profileOptions(rule.simple_profile)}</select></td>
        <td><select data-mc-route="normal">${profileOptions(rule.normal_profile)}</select></td>
        <td><select data-mc-route="complex">${profileOptions(rule.complex_profile)}</select></td>
        <td><select data-mc-route="fallback">${profileOptions(rule.fallback_profile)}</select></td>
        <td><span class="mc-capability-note">${e(capabilityText(rule.required_capabilities || stage.required_capabilities))}</span><label class="mc-inline-check"><input type="checkbox" data-mc-route="enabled"${checked(rule.enabled !== false)}>启用</label></td>
      </tr>`;
    }).join('');
    return `<section class="mc-card mc-routing"><div class="mc-section-head"><div><span class="mc-kicker">阶段路由</span><h2>按任务难度选择模型</h2><p>复杂度由后端根据问题长度、子问题数量、对象数量、附件和证据要求轻量计算，不额外消耗一次大模型调用。</p></div></div><div class="mc-table-wrap"><table class="mc-table"><thead><tr><th>Agent 阶段</th><th>简单</th><th>常规</th><th>复杂</th><th>回退</th><th>能力要求</th></tr></thead><tbody>${rows}</tbody></table></div><div class="mc-routing-foot"><span>未配置或能力不匹配时，运行时自动使用“回退”档案；回退也不可用时继续使用现有 Qwen 配置。</span><button type="button" class="mc-btn primary" data-mc="save-routing">保存阶段路由</button></div></section>`;
  }

  function summary() {
    const profiles = registry?.profiles || [];
    const ready = profiles.filter(item => item.ready).length;
    const enabled = profiles.filter(item => item.enabled !== false).length;
    const stages = registry?.stages?.length || 0;
    return `<div class="mc-summary"><div class="mc-stat"><span>模型档案</span><strong>${profiles.length}</strong><small>${enabled} 个已启用</small></div><div class="mc-stat"><span>可用模型</span><strong>${ready}</strong><small>密钥和地址校验通过</small></div><div class="mc-stat"><span>可配置阶段</span><strong>${stages}</strong><small>每阶段支持三档难度</small></div><div class="mc-stat mc-stat-note"><span>运行原则</span><strong>安全回退</strong><small>新档案未配好不会中断现有 Qwen</small></div></div>`;
  }

  function value(id) {
    return document.getElementById(id)?.value?.trim() || '';
  }

  function numberValue(id, fallback) {
    const number = Number(value(id));
    return Number.isFinite(number) ? number : fallback;
  }

  function profilePayload() {
    const thinking = value('mc-enable_thinking');
    return {
      profile_id: value('mc-profile_id'),
      name: value('mc-name'),
      provider: value('mc-provider') || 'OpenAI-compatible',
      model: value('mc-model'),
      base_url: value('mc-base_url'),
      base_url_env: value('mc-base_url_env'),
      api_key_env: value('mc-api_key_env'),
      capabilities: value('mc-capabilities').split(',').map(item => item.trim()).filter(Boolean),
      cost_tier: value('mc-cost_tier') || 'medium',
      temperature: numberValue('mc-temperature', 0),
      max_tokens: numberValue('mc-max_tokens', 8192),
      timeout_seconds: numberValue('mc-timeout_seconds', 60),
      max_retries: numberValue('mc-max_retries', 1),
      enable_thinking: thinking === '' ? null : thinking === 'true',
      enabled: Boolean(document.getElementById('mc-enabled')?.checked),
      description: value('mc-description')
    };
  }

  async function api(path, options = {}) {
    return fetchJson(path, {
      method: options.method || 'GET',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });
  }

  async function loadRegistry() {
    registry = await api('/api/model-registry');
    if (creatingNew) return;
    const ids = (registry.profiles || []).map(item => item.profile_id);
    if (selectedId && ids.includes(selectedId)) return;
    selectedId = ids.includes('balanced') ? 'balanced' : ids[0] || null;
  }

  function collectRouting() {
    return [...document.querySelectorAll('[data-mc-stage]')].map(row => {
      const stage = row.dataset.mcStage;
      const meta = stageById(stage) || {};
      const old = routingByStage(stage) || defaultRule(meta);
      const select = key => row.querySelector(`[data-mc-route="${key}"]`)?.value || 'balanced';
      return {
        stage,
        name: meta.name || old.name || stage,
        simple_profile: select('simple'),
        normal_profile: select('normal'),
        complex_profile: select('complex'),
        fallback_profile: select('fallback'),
        required_capabilities: old.required_capabilities || meta.required_capabilities || [],
        enabled: Boolean(row.querySelector('[data-mc-route="enabled"]')?.checked)
      };
    });
  }

  async function saveProfile() {
    const payload = profilePayload();
    if (!payload.profile_id || !payload.name || !payload.model) throw new Error('模型 ID、显示名称和模型名称不能为空');
    const exists = Boolean(profileById(payload.profile_id));
    const endpoint = exists ? `/api/model-profiles/${encodeURIComponent(payload.profile_id)}` : '/api/model-profiles';
    const data = await api(endpoint, { method: exists ? 'PUT' : 'POST', body: payload });
    selectedId = data.profile?.profile_id || payload.profile_id;
    creatingNew = false;
    profileDirty = false;
    registry = data.registry || registry;
    message = '模型档案已保存；运行时会按新的档案和阶段路由选择模型。';
    messageKind = 'success';
  }

  async function saveRouting() {
    const data = await api('/api/model-registry/routing', { method: 'PUT', body: { routing: collectRouting() } });
    registry = data;
    message = '阶段路由已保存。新请求开始后生效，正在执行的请求不切换模型。';
    messageKind = 'success';
  }

  async function testProfile() {
    const prompt = value('mc-test-prompt') || '请用一句话说明你适合处理什么任务。';
    const resultNode = document.getElementById('mc-test-result');
    if (resultNode) {
      resultNode.className = 'mc-result';
      resultNode.textContent = '正在调用模型测试连接，请稍候…';
    }
    const data = await api('/api/model-registry/test', { method: 'POST', body: { profile_id: selectedId, prompt } });
    if (resultNode) {
      resultNode.classList.toggle('error', data.status !== 'success');
      const diagnostics = (data.diagnostics || []).map(item => `• ${item.message || item}`).join('\n');
      resultNode.textContent = data.status === 'success'
        ? `连接成功 · ${data.model || selectedId} · ${data.duration_ms ?? '—'} ms\n\n${data.preview || '模型未返回文本。'}`
        : `连接失败 · ${data.duration_ms ?? '—'} ms\n\n${diagnostics || data.error || '未知错误'}`;
    }
  }

  async function action(kind, button) {
    if (button) button.disabled = true;
    try {
      if (kind === 'new') {
        selectedId = null;
        creatingNew = true;
        profileDirty = false;
        message = '';
        messageKind = '';
        await render();
        return;
      }
      if (kind === 'refresh') {
        profileDirty = false;
        await render();
        return;
      }
      if (kind === 'save-profile') {
        await saveProfile();
        await render();
        return;
      }
      if (kind === 'save-routing') {
        await saveRouting();
        await render();
        return;
      }
      if (kind === 'test-profile') {
        if (profileDirty) {
          throw new Error('当前模型档案有未保存修改，请先点击“保存模型档案”，再测试连接。测试连接只针对服务端已保存配置。');
        }
        await testProfile();
      }
    } catch (error) {
      message = error.message || '模型中心操作失败';
      messageKind = 'error';
      const node = document.getElementById('mc-message');
      if (node) {
        node.textContent = message;
        node.className = `mc-message ${messageKind}`;
      }
    } finally {
      if (button) button.disabled = false;
    }
  }

  function bind(root) {
    root.querySelectorAll('[data-mc-profile]').forEach(button => {
      button.onclick = async () => {
        selectedId = button.dataset.mcProfile;
        creatingNew = false;
        profileDirty = false;
        message = '';
        messageKind = '';
        await render();
      };
    });
    root.querySelectorAll('[data-mc]').forEach(button => {
      button.onclick = () => action(button.dataset.mc, button);
    });
  }

  async function render() {
    const root = document.getElementById('subview');
    if (!root) return;
    root.innerHTML = '<div class="empty-panel">正在读取模型中心配置…</div>';
    try {
      await loadRegistry();
      const body = `${summary()}<div class="mc-notice"><strong>怎么选：</strong>简单问答可使用快速模型；包含多个问题、附件、冲突或证据审计时进入复杂档位；没有配置好的候选模型时系统自动回退当前均衡模型，不影响既有运行链路。</div><div class="mc-layout"><aside class="mc-card mc-list"><div class="mc-list-head"><div><span class="mc-kicker">模型档案</span><h2>可用模型</h2></div><span class="mc-count">${(registry.profiles || []).length} 个</span></div><div class="mc-profile-list">${profileCards()}</div><div class="mc-actions"><button type="button" class="mc-btn" data-mc="new">新建模型</button><button type="button" class="mc-btn" data-mc="refresh">刷新</button></div></aside>${profileEditor()}</div>${routingEditor()}<div id="mc-message" class="mc-message ${e(messageKind)}">${e(message)}</div>`;
      root.innerHTML = subviewShell('models', body, '<button class="subview-btn" data-mc="refresh">刷新配置</button>');
      root.querySelector('#mc-profile-form')?.addEventListener('input', () => {
        profileDirty = true;
        const node = document.getElementById('mc-test-result');
        if (node) {
          node.className = 'mc-result';
          node.textContent = '检测到未保存修改；请先点击“保存模型档案”，再测试连接。';
        }
      });
      bind(root);
    } catch (error) {
      root.innerHTML = subviewShell('models', `<div class="mc-card mc-empty"><strong>模型中心读取失败</strong><p>${e(error.message || '未知错误')}</p><p>请确认当前用户具备管理权限且后端服务已启动。</p></div>`);
    }
  }

  renderSubview = async function (view) {
    if (view !== 'models') return oldRender(view);
    await render();
  };
})();

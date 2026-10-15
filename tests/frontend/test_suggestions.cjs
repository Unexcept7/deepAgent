const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticRoot = path.join(__dirname, '../../app/static');
const source = fs.readFileSync(path.join(staticRoot, 'suggestions.js'), 'utf8');
const html = fs.readFileSync(path.join(staticRoot, 'index_production.html'), 'utf8');

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = '';
    this.textContent = '';
    this.disabled = false;
    this.listeners = {};
    this.attributes = {};
    this.classList = { add: name => { this.className += ` ${name}`; } };
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, reference) {
    node.remove();
    const index = reference ? this.children.indexOf(reference) : -1;
    if (index < 0) this.children.push(node);
    else this.children.splice(index, 0, node);
    node.parentElement = this;
    return node;
  }
  remove() {
    if (this.parentElement) {
      const siblings = this.parentElement.children;
      siblings.splice(siblings.indexOf(this), 1);
      this.parentElement = null;
    }
  }
  hasClass(name) { return this.className.split(/\s+/).includes(name); }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
    if (selector === '*') return descendants;
    if (selector === 'button') return descendants.filter(node => node.tagName === 'BUTTON');
    if (selector === '.answer-suggestions button') {
      return descendants.filter(node => {
        if (node.tagName !== 'BUTTON') return false;
        for (let parent = node.parentElement; parent; parent = parent.parentElement) {
          if (parent.hasClass('answer-suggestions')) return true;
        }
        return false;
      });
    }
    return descendants.filter(node => node.hasClass(selector.slice(1)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  click() { return this.listeners.click?.(); }
}

function renderer() {
  const window = {};
  const context = vm.createContext({ window, document: { createElement: tag => new Element(tag) } });
  vm.runInContext(source, context);
  const bubble = new Element('div');
  const answer = new Element('div');
  answer.className = 'answer-markdown';
  const meta = new Element('div');
  meta.className = 'answer-meta';
  bubble.appendChild(answer);
  bubble.appendChild(meta);
  return { ...window.DeepAgentSuggestions, bubble };
}

test('renders specific clarification choices above metadata and sends their full query', async () => {
  const { render, bubble } = renderer();
  const queries = [];
  render({
    evidence: { status: 'context_ambiguous' },
    suggestions: [{ text: 'XP3018 的深度是多少？', query: 'XP3018 的深度是多少？' }],
  }, bubble, { onSelect: query => queries.push(query) });
  const group = bubble.querySelector('.answer-suggestions');
  assert.equal(group.attributes['aria-label'], '推荐提问');
  assert.equal(group.querySelector('.answer-suggestions-heading').children[0].textContent, '请选择查询对象');
  assert.equal(group.children[0].children[0].textContent, '请选择查询对象');
  assert.equal(bubble.children[1], group);
  const button = group.querySelector('button');
  assert.equal(button.type, 'button');
  await button.click();
  assert.deepEqual(queries, ['XP3018 的深度是多少？']);
});

test('supports historical strings, removes duplicates, and renders untrusted text as text', () => {
  const { render, bubble } = renderer();
  const unsafe = '<img src=x onerror=alert(1)>';
  render({ suggestions: [null, '', 'M120 深度是多少？', { text: 'M120 深度是多少？' },
    { text: unsafe, query: 'M160 深度是多少？' }] }, bubble, { onSelect: () => {} });
  const buttons = bubble.querySelectorAll('button');
  assert.equal(buttons.length, 2);
  assert.equal(buttons[1].textContent, unsafe);
  assert.equal(buttons[1].children.length, 0);
  render({ suggestions: Array.from({ length: 8 }, (_, index) => `M${index} 深度是多少？`) },
    bubble, { onSelect: () => {} });
  assert.equal(bubble.querySelectorAll('.answer-suggestions').length, 1);
  assert.equal(bubble.querySelectorAll('button').length, 4);
  render({ suggestions: [] }, bubble, { onSelect: () => {} });
  assert.equal(bubble.querySelectorAll('.answer-suggestions').length, 0);
});

test('busy state and repeated clicks cannot submit concurrent questions', async () => {
  const { render, setBusy, bubble } = renderer();
  let busy = true;
  let calls = 0;
  let finish;
  render({ suggestions: ['M120 的深度是多少？', 'M160 的深度是多少？'] }, bubble, {
    isBusy: () => busy,
    onSelect: () => { calls++; return new Promise(resolve => { finish = resolve; }); },
  });
  const buttons = bubble.querySelectorAll('button');
  assert.equal(buttons[0].disabled, true);
  await buttons[0].click();
  assert.equal(calls, 0);
  busy = false;
  setBusy(bubble, false);
  const pending = buttons[0].click();
  assert.equal(buttons[1].disabled, true);
  await buttons[1].click();
  assert.equal(calls, 1);
  finish();
  await pending;
  assert.equal(buttons[0].disabled, false);
});

test('failed submission re-enables choices', async () => {
  const { render, bubble } = renderer();
  render({ suggestions: ['M120 的深度是多少？'] }, bubble, {
    onSelect: () => Promise.reject(new Error('test failure')),
  });
  const button = bubble.querySelector('button');
  await assert.rejects(button.click(), /test failure/);
  assert.equal(button.disabled, false);
});

function workbench() {
  const requests = [];
  const statuses = new Map();
  const attachment = { name: 'draft.png', data_url: 'draft-image' };
  const state = {
    busy: false, conversationId: 'current-conversation', workflowId: 'current-workflow',
    workflow: { version: 7, params: { source_types: ['document'] } },
    sourceTypes: new Set(['document', 'graph']), attachments: [attachment],
  };
  let attachmentReads = 0;
  let restored = 0;
  const result = { answer: 'test answer', trace: { steps: [] } };
  const context = vm.createContext({
    state, query: { value: '尚未发送的草稿' }, send: { disabled: false }, messages: new Element('div'),
    window: { DeepAgentSuggestions: { setBusy: () => {} } },
    IMAGE_ONLY_PROMPT: '分析图片', TextDecoder, TextEncoder,
    $: selector => { if (!statuses.has(selector)) statuses.set(selector, {}); return statuses.get(selector); },
    submittedAttachments: () => { attachmentReads++; return [...state.attachments]; },
    clearAttachments: () => { state.attachments = []; },
    restoreAttachments: attachments => { restored++; state.attachments = attachments; },
    clearEmpty: () => {}, addMessage: () => new Element('div'), userMessageMarkup: () => '',
    setTrace: () => {}, createConversation: () => { throw new Error('Must reuse the current conversation'); },
    setConversationId: id => { state.conversationId = id; },
    handleWorkflowNodeEvent: () => {}, renderResult: () => {}, loadConversations: () => {},
    parseSse: (_block, handler) => handler('final', result),
    fetch: async (_url, options) => {
      requests.push(JSON.parse(options.body));
      let done = false;
      return { ok: true, body: { getReader: () => ({ read: async () => {
        if (done) return { done: true };
        done = true;
        return { done: false, value: new TextEncoder().encode('event: final\ndata: {}\n\n') };
      } }) } };
    },
  });
  const start = html.indexOf('async function runMultimodal(');
  const end = html.indexOf('// 旧版脚本', start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(html.slice(start, end), context);
  return { context, requests, attachment, attachmentReads: () => attachmentReads, restored: () => restored };
}

test('click-to-ask uses the current conversation and workflow without consuming a draft or images', async () => {
  const env = workbench();
  await env.context.runMultimodal('XP3018 的深度是多少？');
  const request = env.requests[0];
  assert.equal(request.query, 'XP3018 的深度是多少？');
  assert.equal(request.conversation_id, 'current-conversation');
  assert.equal(request.workflow_id, 'current-workflow');
  assert.equal(request.workflow_version, 7);
  assert.deepEqual(request.source_types, ['document']);
  assert.deepEqual(request.attachments, []);
  assert.equal(env.context.query.value, '尚未发送的草稿');
  assert.equal(env.context.state.attachments[0], env.attachment);
  assert.equal(env.attachmentReads(), 0);
  assert.equal(env.context.state.busy, false);
});

test('ordinary composer submission still sends and clears its text and images', async () => {
  const env = workbench();
  await env.context.runMultimodal();
  assert.equal(env.requests[0].query, '尚未发送的草稿');
  assert.equal(env.requests[0].attachments[0].name, 'draft.png');
  assert.equal(env.context.query.value, '');
  assert.equal(env.context.state.attachments.length, 0);
  assert.equal(env.attachmentReads(), 1);
});

test('a running task cannot be overwritten by a suggestion', async () => {
  const env = workbench();
  env.context.state.busy = true;
  await env.context.runMultimodal('XP3018 的深度是多少？');
  assert.equal(env.requests.length, 0);
  assert.equal(env.context.query.value, '尚未发送的草稿');
  assert.equal(env.context.state.attachments[0], env.attachment);
});

test('failed click-to-ask preserves unsent images and re-enables the composer', async () => {
  const env = workbench();
  env.context.fetch = async () => { throw new Error('test network error'); };
  await env.context.runMultimodal('XP3018 的深度是多少？');
  assert.equal(env.context.query.value, '尚未发送的草稿');
  assert.equal(env.context.state.attachments[0], env.attachment);
  assert.equal(env.restored(), 0);
  assert.equal(env.context.send.disabled, false);
  assert.equal(env.context.state.busy, false);
});

test('production rendering and historical messages use the shared suggestions widget', () => {
  assert.ok(html.includes('src="/assets/suggestions.js?v=20261009-1"'));
  assert.ok(html.includes('href="/assets/suggestions.css?v=20261009-1"'));
  assert.ok(html.includes('window.DeepAgentSuggestions?.render(result,bubble,{onSelect:runMultimodal,isBusy:()=>state.busy})'));
  assert.ok(html.includes('item.citations||item.trace||item.suggestions?.length'));
});

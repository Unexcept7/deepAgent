(function(){
  'use strict';

  viewMeta.dictionaries={
    title:'词典中心',
    desc:'维护企业术语、查询属性和故障码识别词；只有发布版本才会进入运行时。'
  };

  const oldRender=renderSubview;
  const e=escapeHtml;
  const types={
    technical_subject:'技术主题',
    aspect:'查询属性',
    stop_exact:'精确排除词',
    stop_prefix:'前缀排除词',
    fault_code:'故障码前缀',
    fault_context:'故障码上下文',
    device_alias:'设备别名',
    model_alias:'型号别名'
  };
  const typeHints={
    technical_subject:'协议、接口和技术主题的标准名称。',
    aspect:'用户希望查询的属性或信息类别。',
    stop_exact:'排除容易被误识别成设备型号的技术值。',
    stop_prefix:'排除以指定前缀开头的技术值或地址。',
    fault_code:'故障码前缀，后端会按数字位数继续识别。',
    fault_context:'用于识别“故障码、报警码”等语境。',
    device_alias:'企业内部设备简称与标准设备名称的映射。',
    model_alias:'型号别名与统一型号或设备 ID 的映射。'
  };
  const aspectNames={
    function_code:'功能码',register_address:'寄存器/地址',message_frame:'报文/数据帧',
    channels_io:'数字量输入输出',channels_generic:'通道数量',communication_parameters:'通信参数',
    protocol_interface:'协议与接口',electrical:'电气参数',performance:'性能参数',
    physical_environment:'尺寸与环境',version:'版本信息',manufacturer:'厂商信息',document_presence:'文档是否存在'
  };

  let selected=null;
  let current=null;
  let items=[];
  let dirty=false;
  let creating=false;
  let notice='';
  let advancedMode=false;
  let entryFilter='all';
  let entrySearch='';
  let selectedEntryIndex=null;
  let compareState={result:null,filter:'changed'};

  const button=(action,label,primary=false)=>`<button type="button" class="dc-btn${primary?' primary':''}" data-dc="${action}">${label}</button>`;
  const value=id=>document.getElementById('dc-'+id)?.value||'';

  function blank(){
    return {
      dictionary_id:null,
      name:'新建业务词典',
      description:'维护企业知识问答的业务词典。',
      enabled:true,
      version:null,
      published_version:null,
      status:'draft',
      entries:[]
    };
  }
  function status(p){return p.enabled===false?'off':p.status==='published'?'published':'draft';}
  function statusText(p){return p.enabled===false?'已停用':p.status==='published'?'已发布':'草稿';}
  function typeLabel(type){return types[type]||type||'未分类';}
  function cleanTerms(input){
    if(Array.isArray(input)) return input.map(item=>String(item||'').trim()).filter(Boolean).filter((item,index,array)=>array.indexOf(item)===index);
    return String(input||'').split(/[\n,，、;；]+/).map(item=>item.trim()).filter(Boolean).filter((item,index,array)=>array.indexOf(item)===index);
  }
  function termsText(input){return Array.isArray(input)?input.join('\n'):'';}
  function typeOptions(selectedType){return Object.entries(types).map(([key,label])=>`<option value="${key}" ${key===selectedType?'selected':''}>${e(label)}</option>`).join('');}
  function canonicalLabel(entry){
    if(entry.dictionary_type==='aspect') return '系统键名';
    if(entry.dictionary_type==='fault_code') return '故障码前缀';
    if(entry.dictionary_type==='stop_exact') return '排除值';
    if(entry.dictionary_type==='stop_prefix') return '排除前缀';
    return '标准名称';
  }
  function aliasLabel(entry){
    if(entry.dictionary_type==='aspect') return '用户问法';
    if(entry.dictionary_type==='fault_context') return '上下文词';
    if(entry.dictionary_type==='stop_exact'||entry.dictionary_type==='stop_prefix') return '补充写法';
    return '别名或常见写法';
  }
  function allSearchText(entry){
    return [entry.canonical_value,entry.description,...(entry.aliases||[]),...(entry.query_terms||[]),...(entry.evidence_terms||[])].join(' ').toLowerCase();
  }
  function entryMatches(entry,index){
    const query=entrySearch.trim().toLowerCase();
    return (entryFilter==='all'||entry.dictionary_type===entryFilter)&&(!query||allSearchText(entry).includes(query)||String(index+1)===query);
  }
  function filteredEntries(){return (current?.entries||[]).map((entry,index)=>({entry,index})).filter(item=>entryMatches(item.entry,item.index));}
  function typeCounts(){
    const counts={all:(current?.entries||[]).length};
    (current?.entries||[]).forEach(item=>{counts[item.dictionary_type]=(counts[item.dictionary_type]||0)+1;});
    return counts;
  }
  function countChips(){
    const counts=typeCounts();
    return Object.entries(types).filter(([key])=>counts[key]).map(([key])=>`<span class="dc-chip">${e(types[key])} <b>${counts[key]}</b></span>`).join('')||'<span class="dc-muted">暂无词条</span>';
  }
  function termsSummary(entry){
    const list=entry.dictionary_type==='aspect'?entry.query_terms:entry.aliases;
    if(!list?.length&&entry.evidence_terms?.length) return `证据词 ${entry.evidence_terms.length} 个`;
    if(!list?.length) return '未配置别名';
    const first=list.slice(0,2).join('、');
    return `${e(first)}${list.length>2?' …':''} <small>${list.length} 个</small>`;
  }
  function newEntry(type){
    return {
      entry_id:'',dictionary_type:type||'technical_subject',canonical_value:'',aliases:[],query_terms:[],
      evidence_terms:[],min_digits:2,max_digits:5,resolved_id:null,description:'',confidence:0.8,
      priority:100,enabled:true,metadata:{}
    };
  }
  function normalizeEntries(entries){
    return entries.map((item,index)=>{
      const result={
        ...item,
        entry_id:String(item.entry_id||`entry-${Date.now()}-${index}`).trim(),
        dictionary_type:String(item.dictionary_type||'technical_subject'),
        canonical_value:String(item.canonical_value||'').trim(),
        aliases:cleanTerms(item.aliases),query_terms:cleanTerms(item.query_terms),evidence_terms:cleanTerms(item.evidence_terms),
        description:String(item.description||'').trim(),resolved_id:String(item.resolved_id||'').trim()||null,
        min_digits:Number(item.min_digits||2),max_digits:Number(item.max_digits||5),
        confidence:Number(item.confidence??0.8),priority:Number(item.priority??100),enabled:item.enabled!==false,metadata:item.metadata||{}
      };
      if(!result.canonical_value) throw new Error(`第 ${index+1} 条词条缺少标准值`);
      if(result.min_digits>result.max_digits) throw new Error(`第 ${index+1} 条故障码词条的数字位数范围不正确`);
      return result;
    });
  }

  function dictionaryPane(){
    return `<aside class="dc-panel dc-dictionary-pane">
      <div class="dc-pane-head"><div><span class="dc-eyebrow">配置目录</span><h2>业务词典</h2></div><span class="dc-count">${items.length}</span></div>
      <div class="dc-pane-actions">${button('new','新建词典')}${button('refresh','刷新')}</div>
      <div class="dc-dictionary-list">${items.length?items.map(item=>`<button type="button" class="dc-dictionary-item ${item.dictionary_id===selected&&!creating?'selected':''}" data-dc-select="${e(item.dictionary_id)}"><span class="dc-dictionary-item-head"><b>${e(item.name)}</b><i class="dc-mini-status ${item.status==='published'?'published':item.enabled===false?'off':''}">${e(item.status==='published'?'已发布':item.enabled===false?'已停用':'草稿')}</i></span><small>v${e(item.version||'—')} · ${e(item.entry_count||0)} 条词条</small><small>${item.dictionary_id==='enterprise-default'?'运行时正在使用':'尚未绑定运行时'}</small></button>`).join(''):'<div class="dc-empty-mini">暂无词典<br>点击“新建词典”开始配置。</div>'}</div>
      <div class="dc-pane-foot"><span class="dc-runtime-dot"></span><span>运行时词典</span><b>${items.some(item=>item.dictionary_id==='enterprise-default')?'enterprise-default':'未绑定'}</b></div>
    </aside>`;
  }

  function categoryNav(){
    const counts=typeCounts();
    const rows=[['all','全部词条'],...Object.entries(types)];
    return `<nav class="dc-category-nav" aria-label="词条分类">${rows.map(([key,label])=>`<button type="button" class="dc-category ${entryFilter===key?'selected':''}" data-dc-filter="${key}"><span><i class="dc-category-icon">${key==='all'?'▦':key==='aspect'?'A':key==='fault_code'?'!':'•'}</i>${e(label)}</span><b>${counts[key]||0}</b></button>`).join('')}</nav>`;
  }

  function catalogRows(){
    const matched=filteredEntries();
    if(!matched.length) return `<tr><td colspan="5" class="dc-table-empty"><strong>没有符合条件的词条</strong><span>更换分类或搜索关键词，也可以新增一条词条。</span></td></tr>`;
    return matched.map(({entry,index})=>`<tr class="${index===selectedEntryIndex?'selected':''}" data-dc-entry-select="${index}"><td><div class="dc-primary-value">${e(entry.canonical_value||'未填写')}</div><div class="dc-secondary-value">${e(entry.entry_id||'待生成 ID')}</div></td><td><span class="dc-type-tag">${e(typeLabel(entry.dictionary_type))}</span></td><td class="dc-alias-cell">${termsSummary(entry)}</td><td><span class="dc-row-status ${entry.enabled===false?'off':''}"><i></i>${entry.enabled===false?'已停用':'启用'}</span></td><td><span class="dc-row-arrow">›</span></td></tr>`).join('');
  }

  function compareVersions(){
    const values=[...(current?.versions||[]).map(item=>Number(item.version)),Number(current?.version)].filter(item=>Number.isFinite(item)&&item>0);
    return [...new Set(values)].sort((a,b)=>a-b);
  }
  function compareVersionOptions(selectedVersion){
    return compareVersions().map(version=>`<option value="${version}" ${Number(selectedVersion)===version?'selected':''}>v${version}</option>`).join('');
  }
  function compareKindLabel(kind){return ({added:'新增',modified:'修改',removed:'删除',unchanged:'未变化'})[kind]||kind;}
  function compareFieldLabel(field){
    return ({name:'词典名称',description:'词典说明',enabled:'启用状态',dictionary_type:'词条类型',canonical_value:'标准值',aliases:'别名',query_terms:'用户问法',evidence_terms:'证据相关词',min_digits:'最少数字位数',max_digits:'最多数字位数',resolved_id:'关联对象 ID',confidence:'置信度',priority:'优先级',metadata:'高级配置'})[field]||field;
  }
  function sameJson(left,right){return JSON.stringify(left??null)===JSON.stringify(right??null);}
  function compareDisplayValue(field,value){
    if(value===undefined||value===null||value==='') return '—';
    if(field==='enabled') return value?'启用':'停用';
    if(Array.isArray(value)) return value.length?value.join('、'):'—';
    if(typeof value==='object') return Object.keys(value).length?JSON.stringify(value,null,2):'—';
    return String(value);
  }
  function compareEntryKey(entry,index){return String(entry?.entry_id||`${entry?.dictionary_type||'entry'}:${entry?.canonical_value||''}:${index}`);}
  function compareEntryTitle(entry){return entry?.canonical_value||entry?.entry_id||'未命名词条';}
  function compareEntryHint(entry){
    if(!entry) return '—';
    const type=typeLabel(entry.dictionary_type);
    const terms=(entry.aliases||entry.query_terms||[]).slice(0,3).join('、');
    return `${type}${terms?` · ${terms}`:''}`;
  }
  function compareRows(result){
    const before=result?.before||{};
    const after=result?.after||{};
    const rows=[];
    ['name','description','enabled'].forEach(field=>{
      if(!sameJson(before[field],after[field])) rows.push({kind:'modified',entry:'词典设置',hint:'基础信息',field,oldValue:compareDisplayValue(field,before[field]),newValue:compareDisplayValue(field,after[field])});
    });
    const beforeEntries=before.entries||[];
    const afterEntries=after.entries||[];
    const beforeMap=new Map(beforeEntries.map((entry,index)=>[compareEntryKey(entry,index),entry]));
    const afterMap=new Map(afterEntries.map((entry,index)=>[compareEntryKey(entry,index),entry]));
    const keys=[...new Set([...beforeEntries.map((entry,index)=>compareEntryKey(entry,index)),...afterEntries.map((entry,index)=>compareEntryKey(entry,index))])];
    const fields=['dictionary_type','canonical_value','aliases','query_terms','evidence_terms','min_digits','max_digits','resolved_id','description','confidence','priority','enabled','metadata'];
    keys.forEach(key=>{
      const oldEntry=beforeMap.get(key);
      const newEntry=afterMap.get(key);
      if(!oldEntry){
        rows.push({kind:'added',entry:compareEntryTitle(newEntry),hint:compareEntryHint(newEntry),field:'整条词条',oldValue:'—',newValue:'已新增'});
        return;
      }
      if(!newEntry){
        rows.push({kind:'removed',entry:compareEntryTitle(oldEntry),hint:compareEntryHint(oldEntry),field:'整条词条',oldValue:'已存在',newValue:'—'});
        return;
      }
      const changed=fields.filter(field=>!sameJson(oldEntry[field],newEntry[field]));
      if(!changed.length){
        rows.push({kind:'unchanged',entry:compareEntryTitle(newEntry),hint:compareEntryHint(newEntry),field:'—',oldValue:'—',newValue:'—'});
        return;
      }
      changed.forEach(field=>rows.push({kind:'modified',entry:compareEntryTitle(newEntry),hint:compareEntryHint(newEntry),field:compareFieldLabel(field),oldValue:compareDisplayValue(field,oldEntry[field]),newValue:compareDisplayValue(field,newEntry[field])}));
    });
    return rows;
  }
  function compareSummary(rows){
    const counts={added:0,modified:0,removed:0,unchanged:0};
    rows.forEach(row=>{counts[row.kind]=(counts[row.kind]||0)+1;});
    return {counts,changed:counts.added+counts.modified+counts.removed};
  }
  function compareValueMarkup(value){return `<span class="dc-compare-value">${e(value)}</span>`;}
  function compareTableMarkup(rows){
    if(!rows.length) return '<div class="dc-compare-empty">当前筛选条件下没有差异。</div>';
    return `<div class="dc-compare-table-wrap"><table class="dc-compare-table"><thead><tr><th>变更</th><th>词条 / 配置</th><th>字段</th><th>旧版本</th><th>新版本</th></tr></thead><tbody>${rows.map(row=>`<tr><td><span class="dc-compare-kind ${row.kind}">${compareKindLabel(row.kind)}</span></td><td><b>${e(row.entry)}</b><small>${e(row.hint||'')}</small></td><td>${e(row.field)}</td><td>${compareValueMarkup(row.oldValue)}</td><td>${compareValueMarkup(row.newValue)}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function renderCompareContent(){
    const result=compareState.result;
    const node=document.getElementById('dc-compare-content');
    if(!node||!result) return;
    const rows=compareRows(result);const summary=compareSummary(rows);
    const visible=rows.filter(row=>compareState.filter==='all'||(compareState.filter==='changed'&&row.kind!=='unchanged')||row.kind===compareState.filter);
    node.innerHTML=`<div class="dc-compare-summary"><div><span>比较范围</span><b>v${e(result.from_version)} → v${e(result.to_version)}</b></div><div><span>发生变更</span><b class="${summary.changed?'has-change':''}">${summary.changed} 项</b></div><div><span>新增 / 修改 / 删除</span><b>${summary.counts.added} / ${summary.counts.modified} / ${summary.counts.removed}</b></div><div><span>未变化词条</span><b>${summary.counts.unchanged}</b></div></div><div class="dc-compare-filterbar"><div><strong>${summary.changed?'已按差异展示':'两个版本内容一致'}</strong><span>点击分类可快速定位变化位置</span></div><div class="dc-compare-filters">${[['changed','有变化'],['all','全部'],['added','新增'],['modified','修改'],['removed','删除'],['unchanged','未变化']].map(([key,label])=>`<button type="button" class="dc-compare-filter ${compareState.filter===key?'selected':''}" data-dc-compare-filter="${key}">${label}<b>${key==='changed'?summary.changed:key==='all'?rows.length:summary.counts[key]}</b></button>`).join('')}</div></div>${compareTableMarkup(visible)}<details class="dc-compare-raw"><summary>高级详情：查看原始对比 JSON</summary><pre>${e(JSON.stringify(result,null,2))}</pre></details>`;
  }
  function renderCompareResult(result){compareState.result=result;compareState.filter='changed';renderCompareContent();}
  function compareDialog(){
    const versions=compareVersions();
    const to=Number(current?.version)||versions[versions.length-1]||'';
    const from=versions.length>1?versions[versions.length-2]:to;
    return `<div id="dc-compare-modal" class="dc-compare-modal" hidden><div class="dc-compare-backdrop" data-dc="close-compare"></div><section class="dc-compare-dialog" role="dialog" aria-modal="true" aria-labelledby="dc-compare-title"><header class="dc-compare-dialog-head"><div><span class="dc-eyebrow">版本管理</span><h2 id="dc-compare-title">对比词典版本</h2><p>用字段级变化查看哪些词条被新增、修改或删除，不需要阅读整段 JSON。</p></div><button type="button" class="dc-compare-close" data-dc="close-compare" aria-label="关闭版本对比">×</button></header><div class="dc-compare-controls"><label><span>起始版本</span><select id="dc-compare-from">${compareVersionOptions(from)}</select></label><span class="dc-compare-arrow">→</span><label><span>目标版本</span><select id="dc-compare-to">${compareVersionOptions(to)}</select></label>${button('run-compare','生成对比',true)}</div><div id="dc-compare-content" class="dc-compare-content"><div class="dc-compare-placeholder"><b>选择两个版本后生成可读差异</b><span>对比结果会按词条和字段展示，原始 JSON 已放到最下方的高级详情。</span></div></div></section></div>`;
  }

  function catalogPane(){
    const p=current||blank();
    const s=status(p);
    const matched=filteredEntries();
    return `<main class="dc-panel dc-catalog-pane">
      <div class="dc-catalog-head"><div class="dc-catalog-title"><div class="dc-title-line"><h2>${e(p.name||'新建业务词典')}</h2><span class="dc-status ${s==='draft'?'draft':s==='off'?'off':''}">${statusText(p)}</span>${p.dictionary_id==='enterprise-default'?'<span class="dc-runtime-badge">运行时使用</span>':''}</div><p>${e(p.description||'暂无说明')}</p></div><div class="dc-version-meta"><span>当前版本</span><b>v${e(p.version||'新建')}</b><span>已发布 v${e(p.published_version||'—')}</span></div></div>
      <div class="dc-basic-strip"><label><span>词典名称</span><input id="dc-name" value="${e(p.name||'')}" maxlength="160"></label><label class="dc-basic-description"><span>说明</span><input id="dc-description" value="${e(p.description||'')}" maxlength="1000" placeholder="给运维人员说明这本词典的用途"></label><label class="dc-enable"><input id="dc-enabled" type="checkbox" ${p.enabled===false?'':'checked'}><span>启用</span></label></div>
      <div class="dc-catalog-body"><div class="dc-category-column"><div class="dc-section-caption">词条分类</div>${categoryNav()}<div class="dc-category-note">分类只影响维护和试匹配展示，不会修改后端固定的安全规则。</div></div><div class="dc-table-column"><div class="dc-table-toolbar"><div><b>词条</b><span id="dc-filter-count">显示 ${matched.length} / ${p.entries?.length||0} 条</span></div><div class="dc-table-tools"><input id="dc-entry-search" value="${e(entrySearch)}" placeholder="搜索标准值、别名或说明"><button type="button" class="dc-filter-button" data-dc="toggle-advanced">${advancedMode?'表单模式':'查看 JSON'}</button>${button('add-entry','新增词条',true)}</div></div><div class="dc-table-wrap"><table class="dc-entry-table"><thead><tr><th>标准值</th><th>类型</th><th>别名 / 用户问法</th><th>状态</th><th></th></tr></thead><tbody id="dc-table-body">${catalogRows()}</tbody></table></div></div></div>
      <div class="dc-catalog-foot"><div class="dc-chip-row">${countChips()}</div><div class="dc-actions">${button('save','保存草稿',true)}${button('publish','发布当前版本')}${button('rollback','回滚')}${button('compare','对比版本')}</div></div>
      <div id="dc-message" class="dc-message ${notice?'visible':''}">${e(notice)}</div><div id="dc-diff"></div>
    </main>`;
  }

  function inspectorField(label,field,valueText,options={}){
    const type=options.type||'text';
    const required=options.required?' <i>*</i>':'';
    const cls=options.className?` ${options.className}`:'';
    if(type==='textarea') return `<label class="dc-inspector-field${cls}"><span>${e(label)}${required}</span><textarea data-entry-field="${field}" data-entry-index="${selectedEntryIndex}" rows="${options.rows||3}" placeholder="${e(options.placeholder||'')}" ${options.disabled?'disabled':''}>${e(valueText||'')}</textarea>${options.help?`<small>${e(options.help)}</small>`:''}</label>`;
    if(type==='select') return `<label class="dc-inspector-field${cls}"><span>${e(label)}${required}</span><select data-entry-field="${field}" data-entry-index="${selectedEntryIndex}">${options.options||''}</select>${options.help?`<small>${e(options.help)}</small>`:''}</label>`;
    return `<label class="dc-inspector-field${cls}"><span>${e(label)}${required}</span><input data-entry-field="${field}" data-entry-index="${selectedEntryIndex}" type="${type}" value="${e(valueText??'')}" ${options.min!==undefined?`min="${options.min}"`:''} ${options.max!==undefined?`max="${options.max}"`:''} ${options.step?`step="${options.step}"`:''} ${options.placeholder?`placeholder="${e(options.placeholder)}"`:''} ${options.disabled?'disabled':''}>${options.help?`<small>${e(options.help)}</small>`:''}</label>`;
  }

  function inspector(){
    const pane=document.getElementById('dc-inspector');
    if(!pane) return;
    if(advancedMode){
      pane.innerHTML=`<div class="dc-inspector-head"><div><span class="dc-eyebrow">高级维护</span><h2>词条 JSON</h2></div>${button('toggle-advanced','返回表单')}</div><div class="dc-json-warning"><b>仅供熟悉系统结构的人员使用</b><span>普通运维建议返回表单模式。保存前会校验必填字段和数字位数。</span></div><textarea id="dc-entries" class="dc-json" spellcheck="false">${e(JSON.stringify(current?.entries||[],null,2))}</textarea>`;
      return;
    }
    const entries=current?.entries||[];
    const index=selectedEntryIndex;
    if(index===null||!entries[index]){
      pane.innerHTML=`<div class="dc-inspector-empty"><span class="dc-empty-icon">⌁</span><strong>选择一个词条</strong><p>从中间列表选择词条后，在这里编辑详细配置。</p></div>`;
      return;
    }
    const entry=entries[index];
    const type=entry.dictionary_type||'technical_subject';
    const aspect=type==='aspect';
    const fault=type==='fault_code';
    const aliasField=aspect?'query_terms':'aliases';
    const aliasValue=aspect?termsText(entry.query_terms):termsText(entry.aliases);
    pane.innerHTML=`<div class="dc-inspector-head"><div><span class="dc-eyebrow">词条详情 · ${index+1}</span><h2>${e(entry.canonical_value||'未命名词条')}</h2><span class="dc-inspector-id">${e(entry.entry_id||'保存后自动生成 ID')}</span></div><div class="dc-inspector-head-actions"><button type="button" class="dc-plain-icon" title="复制词条" data-dc-entry="duplicate" data-entry-index="${index}">⧉</button><button type="button" class="dc-plain-icon danger" title="删除词条" data-dc-entry="remove" data-entry-index="${index}">×</button></div></div><div class="dc-inspector-status"><span class="dc-row-status ${entry.enabled===false?'off':''}"><i></i>${entry.enabled===false?'已停用':'启用中'}</span><label><input type="checkbox" data-entry-field="enabled" data-entry-index="${index}" ${entry.enabled===false?'':'checked'}> 此词条参与运行时匹配</label></div><div class="dc-inspector-body"><div class="dc-inspector-hint">${e(typeHints[type]||'维护一条业务词典项。')}</div>${inspectorField('词条类型','dictionary_type','',{type:'select',options:typeOptions(type),help:'类型决定它参与哪一类识别。'})}${inspectorField(canonicalLabel(entry),'canonical_value',entry.canonical_value,{required:true,placeholder:type==='aspect'?'例如：register_address':'例如：Modbus RTU'})}${fault?`<div class="dc-inspector-grid">${inspectorField('数字最少位数','min_digits',entry.min_digits,{type:'number',min:1,max:12})}${inspectorField('数字最多位数','max_digits',entry.max_digits,{type:'number',min:1,max:12})}</div>`:''}${inspectorField(aliasLabel(entry),aliasField,aliasValue,{type:'textarea',rows:4,placeholder:'每行填写一种写法',help:aspect?'用户说“查波特率、看寄存器”时可能出现的问法。':'同一词条的简称、大小写或常见写法。'})}${aspect?inspectorField('证据相关词','evidence_terms',termsText(entry.evidence_terms),{type:'textarea',rows:3,placeholder:'证据片段中可能出现的词，每行一个',help:'帮助证据校验理解同义表达。'}):''}${inspectorField('运维说明','description',entry.description,{type:'textarea',rows:3,placeholder:'说明什么时候应该使用这条词典项。'})}<details class="dc-advanced-fields"><summary>高级字段（一般不需要修改）</summary><div class="dc-inspector-grid">${inspectorField('词条 ID','entry_id',entry.entry_id,{placeholder:'留空则自动生成'})}${inspectorField('关联对象 ID','resolved_id',entry.resolved_id||'',{placeholder:'可选'})}${inspectorField('置信度','confidence',entry.confidence,{type:'number',min:0,max:1,step:0.01})}${inspectorField('优先级','priority',entry.priority,{type:'number',min:0,max:1000})}</div></details></div>`;
  }

  function testPanel(){
    return `<details class="dc-utility-panel"><summary><span><b>试匹配词典</b><small>只验证词典命中，不执行模型、检索或业务 API</small></span><i>展开</i></summary><div class="dc-utility-body"><div class="dc-test-input"><textarea id="dc-test-query" rows="2" placeholder="例如：北辰47 的 E203 是什么意思？">北辰47 的 E203 是什么意思？</textarea><div class="dc-actions">${button('test','试匹配',true)}</div></div><div id="dc-test-result" class="dc-result">输入问题后查看命中的技术主题、查询属性、故障码和排除词。</div><div class="dc-safety"><b>运行边界</b><span>运行时默认读取 enterprise-default 的已发布版本；设备型号正则由后端固定，这里维护的词典只参与分类和语义匹配。</span></div></div></details>`;
  }

  async function api(path='',body,method){
    return fetchJson('/api/dictionaries'+path,{method:method||(body===undefined?'GET':'POST'),headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  }
  function syncTopFields(){
    if(!current) return;
    current.name=value('name').trim()||current.name;
    current.description=value('description').trim();
    current.enabled=Boolean(document.getElementById('dc-enabled')?.checked);
  }
  function syncAdvancedEntries(){
    if(!advancedMode) return;
    let parsed;
    try{parsed=JSON.parse(value('entries')||'[]');}catch(err){throw new Error('词条 JSON 格式不正确：'+err.message);}
    if(!Array.isArray(parsed)) throw new Error('词条 JSON 必须是数组');
    current.entries=normalizeEntries(parsed);
  }
  function payload(){
    syncTopFields();
    syncAdvancedEntries();
    return {dictionary_id:current?.dictionary_id||null,name:current?.name||'企业业务词典',description:current?.description||'',enabled:current?.enabled!==false,entries:normalizeEntries(current?.entries||[]),base_version:current?.version||null};
  }
  async function saveDraft(){
    const data=await api(current?.dictionary_id?'/'+encodeURIComponent(current.dictionary_id):'',payload(),current?.dictionary_id?'PUT':'POST');
    current=data.dictionary;selected=current.dictionary_id;creating=false;dirty=false;return current;
  }

  function refreshCatalog(){
    const body=document.getElementById('dc-table-body');
    if(body) body.innerHTML=catalogRows();
    const count=document.getElementById('dc-filter-count');
    if(count) count.textContent=`显示 ${filteredEntries().length} / ${current?.entries?.length||0} 条`;
    const chips=document.querySelector('.dc-chip-row');
    if(chips) chips.innerHTML=countChips();
    categoryNavRefresh();
  }
  function categoryNavRefresh(){
    const nav=document.querySelector('.dc-category-nav');
    if(!nav) return;
    const counts=typeCounts();
    nav.querySelectorAll('[data-dc-filter]').forEach(node=>{
      const count=node.querySelector('b');
      if(count) count.textContent=counts[node.dataset.dcFilter]||0;
      node.classList.toggle('selected',entryFilter===node.dataset.dcFilter);
    });
  }
  function addEntry(){
    if(!current) return;
    const type=entryFilter==='all'?'technical_subject':entryFilter;
    current.entries=current.entries||[];current.entries.push(newEntry(type));
    selectedEntryIndex=current.entries.length-1;dirty=true;notice='';refreshCatalog();inspector();
  }
  function selectEntry(index){selectedEntryIndex=Number(index);refreshCatalog();inspector();}
  function fieldValue(target){
    if(target.type==='checkbox') return target.checked;
    if(['min_digits','max_digits','confidence','priority'].includes(target.dataset.entryField)) return Number(target.value||0);
    if(['aliases','query_terms','evidence_terms'].includes(target.dataset.entryField)) return cleanTerms(target.value);
    return target.value;
  }
  function mutateEntry(target){
    const index=Number(target.dataset.entryIndex);
    if(!current?.entries?.[index]) return;
    current.entries[index][target.dataset.entryField]=fieldValue(target);dirty=true;notice='';
    const title=document.querySelector('.dc-inspector-head h2');
    if(title&&target.dataset.entryField==='canonical_value') title.textContent=target.value||'未命名词条';
    if(target.dataset.entryField==='dictionary_type'){refreshCatalog();inspector();}
  }

  async function render(){
    const root=document.getElementById('subview');
    root.innerHTML='<div class="empty-panel">正在读取词典配置…</div>';
    try{
      items=(await api()).dictionaries||[];
      const runtimeItem=items.find(item=>item.dictionary_id==='enterprise-default');
      if(!creating&&(!selected||!items.some(item=>item.dictionary_id===selected))) selected=(runtimeItem||items[0])?.dictionary_id||null;
      current=creating?blank():(selected?(await api('/'+encodeURIComponent(selected))).dictionary:null);
      if(!creating&&current?.entries?.length&&(selectedEntryIndex===null||!current.entries[selectedEntryIndex])) selectedEntryIndex=0;
      if(!current) selectedEntryIndex=null;
      root.innerHTML=subviewShell('dictionaries',`<div class="dc-workspace"><div class="dc-console">${dictionaryPane()}${catalogPane()}<aside id="dc-inspector" class="dc-panel dc-inspector-pane"></aside></div>${testPanel()}${compareDialog()}</div>`,'');
      inspector();bind(root);
    }catch(err){
      root.innerHTML=subviewShell('dictionaries',`<div class="dc-panel dc-error">${e(err.message)}<br>请确认当前用户具备管理权限且后端服务已启动。</div>`,button('refresh','重试'));bind(root);
    }
  }

  function bind(root){
    if(root.dataset.dictionaryEventsBound==='true') return;
    root.dataset.dictionaryEventsBound='true';
    root.addEventListener('click',event=>{
      const dict=event.target.closest('[data-dc-select]');
      if(dict){
        if(dirty&&!window.confirm('放弃尚未保存的修改？')) return;
        selected=dict.dataset.dcSelect;creating=false;dirty=false;notice='';advancedMode=false;entryFilter='all';entrySearch='';selectedEntryIndex=null;render();return;
      }
      const filter=event.target.closest('[data-dc-filter]');
      if(filter){entryFilter=filter.dataset.dcFilter;refreshCatalog();return;}
      const compareFilter=event.target.closest('[data-dc-compare-filter]');
      if(compareFilter&&compareState.result){
        compareState.filter=compareFilter.dataset.dcCompareFilter;
        renderCompareContent();
        return;
      }
      const row=event.target.closest('[data-dc-entry-select]');
      if(row){selectEntry(row.dataset.dcEntrySelect);return;}
      const entryAction=event.target.closest('[data-dc-entry]');
      if(entryAction){
        const index=Number(entryAction.dataset.entryIndex??selectedEntryIndex);
        if(!current?.entries?.[index]) return;
        if(entryAction.dataset.dcEntry==='remove'){
          if(!window.confirm(`确定删除“${current.entries[index].canonical_value||'未命名词条'}”吗？发布后会影响运行时识别。`)) return;
          current.entries.splice(index,1);selectedEntryIndex=current.entries.length?Math.min(index,current.entries.length-1):null;dirty=true;refreshCatalog();inspector();
        }else if(entryAction.dataset.dcEntry==='duplicate'){
          const copy=JSON.parse(JSON.stringify(current.entries[index]));copy.entry_id='';copy.canonical_value=copy.canonical_value?`${copy.canonical_value}（副本）`:'';current.entries.splice(index+1,0,copy);selectedEntryIndex=index+1;dirty=true;refreshCatalog();inspector();
        }
        return;
      }
      const actionNode=event.target.closest('[data-dc]');
      if(actionNode) action(actionNode.dataset.dc,actionNode);
    });
    root.addEventListener('input',event=>{
      const target=event.target;
      if(target.id==='dc-entry-search'){entrySearch=target.value;refreshCatalog();return;}
      if(target.id==='dc-entries'){dirty=true;notice='';return;}
      if(target.id==='dc-name'||target.id==='dc-description'){dirty=true;notice='';return;}
      if(target.matches('[data-entry-field]')) mutateEntry(target);
    });
    root.addEventListener('change',event=>{
      const target=event.target;
      if(target.id==='dc-enabled'){dirty=true;notice='';return;}
      if(target.matches('[data-entry-field]')) mutateEntry(target);
    });
  }

  async function action(kind,btn){
    if(btn?.disabled) return;
    if(btn) btn.disabled=true;
    try{
      if(kind==='new'){
        if(dirty&&!window.confirm('放弃尚未保存的修改？')) return;
        creating=true;selected=null;current=blank();selectedEntryIndex=null;dirty=false;notice='';advancedMode=false;entryFilter='all';entrySearch='';await render();return;
      }
      if(kind==='add-entry'){addEntry();return;}
      if(kind==='toggle-advanced'){
        if(advancedMode) syncAdvancedEntries();
        advancedMode=!advancedMode;
        const toggle=document.querySelector('.dc-filter-button');
        if(toggle) toggle.textContent=advancedMode?'表单模式':'查看 JSON';
        inspector();return;
      }
      if(kind==='refresh'){
        if(dirty&&!window.confirm('放弃尚未保存的修改？')) return;
        dirty=false;notice='';advancedMode=false;selectedEntryIndex=null;await render();return;
      }
      if(kind==='save'||kind==='publish'){
        if(!current) current=blank();
        if(kind==='save'||dirty||!current.version) await saveDraft();
        if(kind==='publish'){
          current=(await api('/'+encodeURIComponent(selected)+'/publish',{version:current.version,note:'词典中心发布'})).dictionary;dirty=false;notice='已发布；后续运行会读取该版本。';
        }else notice='草稿已保存，发布后才会进入运行时。';
        await render();return;
      }
      if(kind==='test'){
        if(!selected||!current) throw new Error('请先选择一个词典');
        const query=document.getElementById('dc-test-query')?.value.trim();
        const node=document.getElementById('dc-test-result');
        if(!query){node.textContent='请先输入问题。';node.classList.add('error');return;}
        node.classList.remove('error');node.textContent='正在试匹配…';
        const result=await api('/test',{dictionary_id:selected,version:current.version,query});
        const hits=result.hits||[];
        node.innerHTML=hits.length?hits.map(item=>`<div class="dc-hit"><b>${e(typeLabel(item.dictionary_type))}</b><span>${e(item.canonical_value||'')}</span><small>${e((item.matched_terms||[]).join('、'))}</small></div>`).join(''):'没有命中当前版本词典。';
        return;
      }
      if(kind==='close-compare'){
        const modal=document.getElementById('dc-compare-modal');
        if(modal) modal.hidden=true;
        return;
      }
      if(kind==='compare'){
        if(!selected||!current?.dictionary_id) throw new Error('请先选择一个已保存的词典');
        const modal=document.getElementById('dc-compare-modal');
        if(!modal) throw new Error('版本对比面板加载失败');
        modal.hidden=false;compareState={result:null,filter:'changed'};
        const content=document.getElementById('dc-compare-content');
        if(content) content.innerHTML='<div class="dc-compare-placeholder"><b>选择两个版本后生成可读差异</b><span>对比结果会按词条和字段展示，原始 JSON 已放到最下方的高级详情。</span></div>';
        return;
      }
      if(kind==='run-compare'){
        if(!selected||!current?.dictionary_id) throw new Error('请先选择一个已保存的词典');
        const from=Number(document.getElementById('dc-compare-from')?.value);
        const to=Number(document.getElementById('dc-compare-to')?.value);
        if(!from||!to) throw new Error('请选择起始版本和目标版本');
        const result=await api('/'+encodeURIComponent(selected)+`/compare?from_version=${from}&to_version=${to}`);
        renderCompareResult(result);
        return;
      }
      if(!selected||!current?.dictionary_id) throw new Error('请先保存词典');
      const versions=(current.versions||[]).map(item=>item.version).filter(Boolean);
      if(!versions.length) throw new Error('暂无历史版本');
      const version=Number(window.prompt('可选版本：'+versions.join('、'),String(versions[versions.length-1])));
      if(!version) return;
      if(kind==='rollback'){
        if(dirty&&!window.confirm('回滚将放弃尚未保存的修改，继续？')) return;
        current=(await api('/'+encodeURIComponent(selected)+'/rollback',{version,publish:true,note:'词典中心回滚'})).dictionary;dirty=false;notice='已回滚并发布新版本。';await render();
      }
    }catch(err){
      const node=kind==='test'?document.getElementById('dc-test-result'):document.getElementById('dc-message');
      if(node){node.textContent=err.message||'操作失败';node.classList.add('error','visible');}
    }finally{if(btn) btn.disabled=false;}
  }

  renderSubview=async function(view){if(view!=='dictionaries') return oldRender(view);await render();};
})();

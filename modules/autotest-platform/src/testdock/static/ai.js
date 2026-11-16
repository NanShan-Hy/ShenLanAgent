/* Native assistant UI; all data stays behind the TestDock session API. */
export function createAssistant(ui) {
  const {h,button,input,area,select,field,checkbox,modal,request,toast,act,state,pretty,date}=ui;
  let current=null,sourceRefs=[],selectedFiles=new Set(),events=null,container=null,drawer=null,context=null,linkedRun=null;
  const api=(path,method='GET',body)=>request('/api/ai'+path,method,body);
  const labels={queued:'排队中',running:'分析中',waiting_confirmation:'等待操作确认',completed:'已完成',failed:'未完成',cancelled:'已取消',unknown:'结果未知，需恢复'};
  const operations={'case.create':'新增用例','case.update':'修改用例','suite.create':'新增套件','suite.update':'修改套件','environment.create':'新增环境','environment.update':'修改环境','schedule.create':'新增定时任务','schedule.update':'修改定时任务','schedule.enable':'启停定时任务','run.create':'启动测试','run.matrix':'多环境执行','run.rerun':'失败重跑'};
  const key=()=> 'testdock-ai-'+state.user.id;
  function stop(){if(events){events.close();events=null;}}
  async function ensureConversation(mode='all',projectIds=[]) {
    if(current)return current;
    const data=await api('/conversations','POST',{mode,project_id:mode==='project'?state.project?.id:null,workbench_project_ids:projectIds});
    current=data.id;localStorage.setItem(key(),current);return current;
  }
  function notice(text){return h('p',{class:'ai-notice'},text);}
  function closeDrawer(){stop();drawer?.remove();drawer=null;}
  async function open(options={}) {
    closeDrawer();ui.closeModal();
    if(options.runId){current=null;selectedFiles.clear();sourceRefs=[await api('/source','POST',{system:'testdock',kind:'runs',id:options.runId})];linkedRun=await request('/api/runs/'+options.runId);}
    drawer=h('div',{class:'ai-drawer-backdrop'},h('section',{class:'ai-drawer',role:'dialog','aria-label':'AI 快捷助手','aria-modal':'true'},
      h('div',{class:'ai-drawer-head'},h('strong',{},'AI 快捷助手'),button('展开工作台',()=>{closeDrawer();ui.navigate('ai');},'ghost small'),button('关闭',closeDrawer,'ghost small')),
      h('div',{class:'ai-drawer-body'})));
    drawer.addEventListener('click',e=>{if(e.target===drawer)closeDrawer();});document.body.append(drawer);
    await render(drawer.querySelector('.ai-drawer-body'),true,options.runId?`请分析本次执行报告，区分事实、原因假设和核查建议，并引用证据。`:'');
  }
  async function page(){closeDrawer();const node=h('section',{class:'ai-page'});await render(node);return node;}
  async function refresh(){if(container?.isConnected)await render(container,!!drawer);}
  async function source(ref){const r=await api('/source','POST',ref);const body=h('div',{class:'form-stack'},notice((r.system==='testdock'?'测试平台':'知识工作台')+' · '+(r.locator||'正文')),h('pre',{class:'ai-code'},r.text));if(r.system==='workbench'&&r.can_manage_policy)body.append(button(r.egress==='external'?'限制此资料为本地':'允许此资料外发',()=>act(async()=>{if(r.egress!=='external'&&!confirm('允许此资料的脱敏内容发送给外部模型？祖先来源与项目策略仍需满足。'))return;await api('/source-policy','PUT',{id:r.id,egress:r.egress==='external'?'local':'external'});ui.closeModal();await source(ref);}),'secondary'));modal(r.title||'来源',body,null,true);}
  async function reviewMemory(id){const m=await api('/memory','POST',{action:'view',id});const dialog=modal('审核记忆候选',h('div',{class:'form-stack'},h('strong',{},m.title),h('p',{class:'ai-message-text'},m.body),notice('当前状态：'+m.status+'。确认后才能作为长期记忆参与检索。')),m.status==='pending'?async()=>{await api('/memory','POST',{action:'review',id,version:m.version,decision:'approve'});await refresh();}:null,true);if(m.status==='pending')dialog.querySelector('.modal-foot .button:last-child').textContent='确认记忆生效';}
  async function attachScreenshot(){await ensureConversation();let filename=linkedRun.artifacts.find(f=>/\.(png|jpe?g|webp)$/i.test(f)),consent=false;modal('选择报告截图',h('div',{class:'form-stack'},field('截图',select(filename,linkedRun.artifacts.filter(f=>/\.(png|jpe?g|webp)$/i.test(f)),v=>filename=v)),checkbox('同意将此截图发送给外部模型',consent,v=>consent=v)),async()=>{const f=await api('/conversations/'+current+'/screenshot','POST',{run_id:linkedRun.id,filename,external_consent:consent});selectedFiles.add(f.id);await refresh();});}

  async function fileInfo(id){
    const info=await api('/files/'+id);let sections=(info.selected_sections||[]).join('\n'),consent=info.consent;
    modal('附件与分段',h('div',{class:'form-stack'},h('strong',{},info.name),h('pre',{class:'ai-code'},pretty(info.gateway)),field('使用的页或分段',area(sections,v=>sections=v,{rows:3,placeholder:'每行一个标签，例如 page:101'}),'长文件使用网关返回的分段标签。'),checkbox('同意将此附件发送给外部模型',consent,v=>consent=v)),async()=>{await api('/files/'+id,'PUT',{selected_sections:sections.split(/\n|,/).map(s=>s.trim()).filter(Boolean),external_consent:consent});await refresh();},true);
  }
  async function upload() {
    await ensureConversation();let pid=state.project?.id||state.projects[0]?.id,consent=false;
    const file=h('input',{type:'file',accept:'.png,.jpg,.jpeg,.webp,.pdf,.docx,.xlsx,.csv,.tsv,.txt,.md,.json,.py,.js,.ts,.html,.css,.yaml,.yml,.sql,.log'});
    modal('添加分析附件',h('div',{class:'form-stack'},field('所属项目',select(pid,state.projects.map(p=>[p.id,p.name]),v=>pid=Number(v))),field('附件文件',file,'每个文件最多20MiB；每轮最多4个附件。'),checkbox('同意将此附件发送给外部模型',consent,v=>consent=v),notice('附件保持本人私有。发送还需所属项目允许外部模型处理。')),async()=>{
      if(!file.files[0])throw Error('请选择附件');const body=new FormData();body.append('file',file.files[0]);body.append('project_id',pid);body.append('external_consent',String(consent));
      const r=await fetch('/api/ai/conversations/'+current+'/files',{method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'TestDock'},body});
      if(!r.ok){const e=await r.json();throw Error(typeof e.detail==='string'?e.detail:'附件上传失败');}const result=await r.json();selectedFiles.add(result.id);await refresh();
    });
  }
  async function settings() {
    const d=await api('/settings');d.api_key='';
    const body=h('div',{class:'form-stack'},field('网关 Base URL',input(d.base_url,v=>d.base_url=v,'url')),field('专用应用 Key',input('',v=>d.api_key=v,'password',{autocomplete:'new-password'}),d.key_configured?'已保存密钥，留空保留。':'密钥仅在后端加密保存。'),field('默认模型别名',input(d.model,v=>d.model=v)),field('已验证视觉模型',input(d.vision_models.join(', '),v=>d.vision_models=v.split(',').map(s=>s.trim()).filter(Boolean)),'只填写已在网关验证图片能力的模型别名。'),field('知识工作台地址',input(d.workbench_url,v=>d.workbench_url=v,'url')),checkbox('启用站内 AI 接入',d.enabled,v=>d.enabled=v),notice('连接目录检查不生成内容；真实测试会产生一次小额模型调用。'),button('检查已保存配置的模型目录',()=>act(async()=>{const r=await api('/models');toast('可访问模型：'+(r.data||[]).map(m=>m.id).join('、'));}),'secondary'),button('按已保存配置发起真实测试',()=>act(async()=>{if(!confirm('将发起一次最多128输出Token的真实模型调用，并记录网关用量。继续？'))return;const r=await api('/test','POST',{});current=r.conversation_id;localStorage.setItem(key(),current);ui.closeModal();await refresh();}),'secondary'));
    modal('AI 连接设置',body,async()=>{const value={...d};delete value.key_configured;await api('/settings','PUT',value);await refresh();},true);
  }
  async function knowledgeScope() {
    const data=await api('/context');const body=h('div',{class:'form-stack'},notice('全部范围包含本人可见的测试项目、知识和已确认记忆。项目管理员可设置脱敏测试资产的外发许可；工作台资料仍保留各自的外发限制。'));
    for(const p of data.projects)body.append(h('div',{class:'ai-policy-row'},h('div',{},h('strong',{},p.name),h('small',{},(p.external_id?'TestDock':'知识项目')+' · '+p.role+' · '+(p.egress==='external'?'允许外部模型':'仅本地检索'))),p.role==='admin'?button(p.egress==='external'?'限制为本地':'允许外部模型',()=>act(async()=>{if(p.egress!=='external'&&!confirm('允许该项目的脱敏测试资产及已获许可资料发送给外部云模型？'))return;await api('/project-policy','PUT',{project_id:p.id,egress:p.egress==='external'?'local':'external'});ui.closeModal();await knowledgeScope();}),'secondary small'):null));
    modal('资料范围与外发',body,null,true);
  }
  async function reviewProposal(p) {
    const content=h('div',{class:'form-stack'},notice((state.projects.find(x=>x.id===p.project_id)?.name||'项目 #'+p.project_id)+' · '+operations[p.kind]+' · 有效至 '+date(p.expires_at)),
      p.before?h('details',{},h('summary',{},'当前配置'),h('pre',{class:'ai-code'},pretty(p.before))):null,
      h('h3',{},'将执行的具体配置'),h('pre',{class:'ai-code'},pretty(p.arguments)),p.next_fires?h('div',{},h('strong',{},'未来5次触发（'+p.arguments.timezone+'）'),...p.next_fires.map(t=>h('p',{},new Date(t*1000).toLocaleString('zh-CN',{timeZone:p.arguments.timezone,hour12:false}))),notice(p.arguments.enabled?'确认后该计划会按以上时间持续执行。':'此计划保持关闭。'),notice('通知渠道：'+(p.notifications===null?'沿用项目规则':(p.notifications||[]).join(', ')||'不推送'))):null);
    const dialog=modal('审核操作 · '+operations[p.kind],content,async()=>{await api('/proposals/'+p.id+'/confirm','POST',{});toast('操作已执行，重复确认不会再次提交');await refresh();},true);
    dialog.querySelector('.modal-foot .button:last-child').textContent='确认执行';
  }
  function watch(id,target) {
    stop();events=new EventSource('/api/ai/tasks/'+id+'/events');let text='';
    events.onmessage=e=>{if(!target.isConnected){stop();return;}const value=JSON.parse(e.data);if(value.type==='delta'){text+=value.text;target.textContent=text;target.setAttribute('data-streaming','true');}else if(value.type==='progress'||value.type==='notice'){if(!text)target.textContent=value.text;}else if(value.type==='error'){target.textContent=value.text;stop();act(refresh);}};
    events.addEventListener('state',()=>{stop();act(refresh);});events.addEventListener('revoked',()=>{stop();target.textContent='权限已变化，请刷新资料范围。';});
    events.onerror=()=>{if(!target.isConnected)stop();};
  }
  async function render(target,compact=false,prefill='') {
    stop();const owner=state.user?.id;container=target;target.replaceChildren(notice('正在连接 AI 助手…'));
    const status=await api('/status'),history=await api('/conversations');
    if(!current)current=localStorage.getItem(key());
    if(current&&!history.some(c=>c.id===current)){current=null;sourceRefs=[];selectedFiles.clear();}
    let data=current?await api('/conversations/'+current):null,query=prefill,mode='all';
    const heading=h('div',{class:'ai-page-heading'},h('div',{},h('h1',{},'全局 AI 助手'),h('p',{},'问答、证据与操作，连接你的测试工作。')),h('div',{class:'actions'},button('资料范围',()=>act(knowledgeScope),'secondary small'),status.is_admin?button('AI 设置',()=>act(settings),'secondary small'):null));
    const histories=h('aside',{class:'ai-history'},button('新建对话',()=>{current=null;sourceRefs=[];selectedFiles.clear();localStorage.removeItem(key());linkedRun=null;act(refresh);},'secondary'),...history.map(c=>button(c.title,()=>{current=c.id;sourceRefs=[];selectedFiles.clear();localStorage.setItem(key(),current);act(refresh);},'ghost small'+(current===c.id?' ai-selected':''))));
    const messages=h('div',{class:'ai-messages','aria-live':'polite'});
    if(!data?.messages.length)messages.append(h('div',{class:'ai-welcome'},h('h2',{},'从一个问题开始'),h('p',{},'比较失败原因、查找知识，或提出需要确认的测试操作。'),h('div',{class:'ai-prompts'},...['比较我有权限项目的最近测试失败，并引用证据','根据当前项目接口资料生成测试用例草案','帮我检查定时回归配置，先给出修改预览'].map(t=>button(t,()=>{query=t;queryBox.value=t;},'secondary small')))));
    for(const m of data?.messages||[])messages.append(h('article',{class:'ai-message '+m.role},h('div',{class:'ai-message-label'},m.role==='user'?'你':'AI 助手'),h('div',{class:'ai-message-text'},m.text),m.role==='assistant'&&m.sources?.length?h('div',{class:'ai-citations'},...m.sources.map(r=>button('['+r.citation+'] '+r.title,()=>act(()=>source(r)),'ghost small'))):null));
    if(data?.hidden_messages)messages.append(notice('部分历史因来源权限或版本变化已隐藏。'));
    for(const p of data?.proposals||[])messages.append(h('section',{class:'ai-proposal'},h('div',{},h('strong',{},operations[p.kind]),h('span',{class:'badge'},({pending:'待审核',applied:'已执行',rejected:'已拒绝'})[p.status]||p.status)),h('p',{},(state.projects.find(x=>x.id===p.project_id)?.name||'项目 #'+p.project_id)+' · '+(p.arguments.name||p.arguments.suite_id&&'套件 #'+p.arguments.suite_id||p.target_id||'')),h('div',{class:'actions'},p.status==='pending'?button('审核并确认',()=>act(()=>reviewProposal(p))):null,p.status==='pending'?button('拒绝',()=>act(async()=>{await api('/proposals/'+p.id+'/reject','POST',{});await refresh();}),'secondary'):null,p.result?(p.kind.startsWith('run.')?h('div',{class:'actions'},...(p.result.runs||[p.result]).map(r=>button('查看报告 #'+String(r.id).slice(0,8),()=>act(()=>ui.openReport(r.id)),'secondary small'))):notice((p.result.name||operations[p.kind])+' · #'+p.result.id+' 已保存')):null)));
    for(const t of data?.tasks||[])for(const id of t.memory_candidates||[])messages.append(button('审核记忆候选',()=>act(()=>reviewMemory(id)),'secondary small'));
    const live=data?.tasks.find(t=>['queued','running','waiting_confirmation','unknown'].includes(t.status));const latest=data?.tasks[0];
    const streaming=h('div',{class:'ai-stream'});
    if(live||latest?.error)messages.append(h('div',{class:'ai-task'},h('strong',{},labels[(live||latest).status]),(live||latest).error?notice((live||latest).error):null,streaming,h('div',{class:'actions'},live&&live.status!=='unknown'?button('取消任务',()=>act(async()=>{await api('/tasks/'+live.id+'/cancel','POST',{});await refresh();}),'ghost small'):null,live&&['unknown','waiting_confirmation'].includes(live.status)?button('恢复为新上下文',()=>act(async()=>{if(!confirm('结束本次待处理状态，已发生的操作或费用保留，原模型调用不会重放。继续？'))return;await api('/conversations/'+current+'/recover','POST',{acknowledge_unknown:true});await refresh();}),'secondary small'):null)));
    const files=h('div',{class:'ai-files'},...(data?.files||[]).map(f=>h('div',{class:'ai-file'},checkbox(f.name+' · '+Math.ceil(f.bytes/1024)+' KiB',selectedFiles.has(f.id),v=>v?selectedFiles.add(f.id):selectedFiles.delete(f.id)),button('详情 / 分段',()=>act(()=>fileInfo(f.id)),'ghost small'),button('移除',()=>act(async()=>{if(!confirm('清理此附件及对应网关副本？'))return;await api('/files/'+f.id,'DELETE');selectedFiles.delete(f.id);await refresh();}),'ghost small'))));
    const queryBox=area(query,v=>query=v,{rows:compact?3:4,placeholder:'描述问题或想完成的操作…','aria-label':'AI 问题'});
    mode=data?.scope.mode||mode;const scopeSelect=select(mode,[['all','全部有权资料'],['project','当前测试项目'],['selected','指定知识项目']],v=>mode=v);scopeSelect.disabled=!!current;
    const sendButton=button('发送',()=>act(async()=>{if(!query.trim())throw Error('请输入问题');let pids=[];if(mode==='selected'&&!current){const ctx=await api('/context');const chosen=prompt('请输入知识项目编号（逗号分隔）：\n'+ctx.projects.map(p=>p.id+' '+p.name).join('\n'));if(chosen===null)return;pids=chosen.split(',').map(Number).filter(Number.isFinite);}await ensureConversation(mode,pids);await api('/conversations/'+current+'/messages','POST',{query,sources:sourceRefs,file_ids:[...selectedFiles]});sourceRefs=[];await refresh();}));sendButton.disabled=!!live||!status.enabled||status.revoked;
    const controls=h('div',{class:'ai-composer'},sourceRefs.length?notice('已附加 '+sourceRefs.length+' 份报告或明确来源。'):null,files,linkedRun?.artifacts?.some(f=>/\.(png|jpe?g|webp)$/i.test(f))?button('附加失败截图',()=>act(attachScreenshot),'secondary small'):null,queryBox,h('div',{class:'ai-composer-actions'},scopeSelect,button('添加附件',()=>act(upload),'secondary small'),button('本地检索',()=>act(async()=>{if(!query.trim())throw Error('请输入关键词');const result=await api('/search','POST',{query});modal('本地检索 · '+result.mode,h('div',{class:'form-stack'},...result.items.map(r=>h('div',{class:'ai-search-hit'},h('strong',{},r.title),h('p',{},r.text),button('查看来源',()=>act(()=>source(r)),'ghost small'),button('用于本轮分析',()=>{sourceRefs.push(r);ui.closeModal();act(refresh);},'secondary small'))),!result.items.length?notice('未检索到可见资料。'):null),null,true);}),'secondary small'),sendButton));
    const banner=!status.enabled?notice('AI 模型接入尚未启用。可以使用本地检索，管理员可在 AI 设置中配置网关。'):notice((status.worker_online?'AI 执行器在线':'AI 执行器暂未连接')+' · '+status.model+' · '+(data?'当前会话范围：'+({all:'全部有权资料',project:'指定测试项目',selected:'指定知识项目'})[data.scope.mode]:'新会话可选择资料范围'));
    const reconnect=status.revoked?button('重新连接 AI 身份',()=>act(async()=>{await api('/reconnect','POST',{});await refresh();}),'secondary'):null;
    const clear=current?button('清理此对话',()=>act(async()=>{if(!confirm('清理本人此对话、附件及网关托管历史？测试业务与费用记录保留。'))return;await api('/conversations/'+current,'DELETE');current=null;sourceRefs=[];selectedFiles.clear();localStorage.removeItem(key());await refresh();}),'ghost small'):null;
    if(state.user?.id!==owner)return;target.replaceChildren(...[heading,banner,reconnect,h('div',{class:'ai-layout'+(compact?' compact':'')},histories,h('div',{class:'ai-main'},h('div',{class:'ai-conversation-title'},h('strong',{},data?.title||'新对话'),clear),messages,controls))].filter(Boolean));
    if(live&&['queued','running'].includes(live.status))watch(live.id,streaming);
  }
  function reset(){closeDrawer();current=null;sourceRefs=[];selectedFiles.clear();linkedRun=null;if(container)container.replaceChildren();container=null;}
  return {page,open,close:closeDrawer,reset};
}

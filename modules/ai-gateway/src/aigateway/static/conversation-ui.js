const labels={active:'可继续',waiting_tools:'等待外部工具结果',needs_recovery:'需要明确恢复',closed:'已关闭',cleared:'正文已清理'};
const policyLabels={strict:'严格固定账号',continuity:'上下文连续优先'};
const size=n=>(Number(n||0)/1048576).toFixed(2)+' MiB';

export async function conversationPage(ui){
 const {h,api,state,panel,table,btn,field,input,select,stat,editor,render,date}=ui;
 const [settings,result,users,files]=await Promise.all([api('/admin/conversation-settings','GET',undefined,false),api('/admin/conversations?page='+state.opsPage+'&'+new URLSearchParams(state.conversationFilters||{}),'GET',undefined,false),api('/admin/users','GET',undefined,false),api('/admin/gateway-files?page='+(state.filePage||1),'GET',undefined,false)]);
 const user=select(state.conversationFilters?.user_id||'',[['','全部用户'],...users.map(u=>[u.id,u.display_name+' · '+u.username])]);
 const app=select(state.conversationFilters?.application_id||'',[['','全部应用'],...settings.applications.map(a=>[a.id,a.name])]);
 const task=input(state.conversationFilters?.task_type||'');
 const controls=h('div',{class:'form-grid'},field('会话用户',user),field('会话应用',app),field('任务类型',task),btn('筛选会话',async()=>{state.conversationFilters={user_id:user.value,application_id:app.value,task_type:task.value};state.opsPage=1;await render();},'secondary'));
 const sessions=panel('任务会话',table(['用户 / 应用','任务 / 会话','状态 / 版本','调度策略','更新时间','操作'],result.items,r=>[
  h('div',{},r.display_name,h('span',{class:'muted'},r.application_name),h('span',{class:'muted'},'外部用户摘要 '+r.subject_hash)),
  h('div',{},r.task_type,h('span',{class:'muted'},r.id),h('span',{class:'muted'},r.model)),
  h('div',{},labels[r.state]||r.state,h('span',{class:'muted'},'版本 '+r.revision+(r.busy?' · 正在处理':''))),
  h('div',{},policyLabels[r.policy],h('span',{class:'muted'},r.binding_state)),date(r.updated_at),
  h('div',{class:'actions'},btn('查看会话',()=>detail(ui,r.id),'ghost small'),btn('清理会话',()=>cleanup(ui,r.id),'ghost small'))]),'托管历史独立于请求审计；MCP在接入方执行。',btn('刷新',render,'secondary small'));
 if(result.total>20)sessions.append(h('div',{class:'pager'},'共 '+result.total+' 条',btn('上一页',async()=>{state.opsPage=Math.max(1,state.opsPage-1);await render();},'secondary small'),btn('下一页',async()=>{if(state.opsPage*20<result.total){state.opsPage++;await render();}},'secondary small')));
 function policyEditor(value={application_id:'',task_type:'',policy:'continuity',identity_mode:'key_owner'}){
  return editor('会话调度规则',[
   {key:'application_id',label:'规则应用',type:'select',options:[['','平台默认'],...settings.applications.map(a=>[a.id,a.name])]},
   {key:'task_type',label:'规则任务类型',help:'留空为应用默认；同类型默认续用会话，显式新建可分开。'},
   {key:'policy',label:'账号故障策略',type:'select',options:Object.entries(policyLabels)},
   {key:'identity_mode',label:'应用身份模式',type:'select',options:[['key_owner','平台账号'],['application_subject','可信后端传外部用户']],help:'外部用户模式在具体应用默认规则配置，不配置在任务类型规则。'}],value,d=>api('/admin/conversation-policies','PUT',d,false));
 }
 const policies=panel('会话与身份规则',table(['应用','任务类型','账号策略','身份模式','操作'],settings.policies,p=>[
  settings.applications.find(a=>a.id===p.application_id)?.name||'平台默认',p.task_type||'默认',policyLabels[p.policy],p.identity_mode==='application_subject'?'外部用户隔离':'平台账号',btn('编辑规则',()=>policyEditor(p),'ghost small')]),'应用＋类型规则优先，其次应用默认，再使用平台规则。已有会话保留创建时策略。',btn('新增规则',()=>policyEditor(),'secondary small'));
 const media=panel('模型图片能力',table(['模型','视觉状态','单图保守Token预算','图片上限','操作'],settings.media,m=>[
  m.alias,m.vision&&m.verified?'已验证启用':'未启用',m.image_tokens,m.max_images,
  btn('配置图片能力',()=>editor('图片能力 · '+m.alias,[{key:'vision',label:'启用图片输入',type:'check'},{key:'verified',label:'已完成该模型媒体与用量验证',type:'check'},{key:'image_tokens',label:'每图保守Token预算',type:'number',required:true,help:'图片归一化至1024像素；按模型实际协议核定，不能按Base64长度计数。'},{key:'max_images',label:'每次最大图片数',type:'number',required:true}],{vision:!!m.vision,verified:!!m.verified,image_tokens:m.image_tokens||16384,max_images:m.max_images},d=>api('/admin/model-media/'+m.id,'PUT',d,false)),'ghost small')]),'不支持的模型明确拒绝图片；普通文本附件先安全解析。');
 const attachments=panel('上传附件',table(['文件','用户 / 应用','大小 / 状态','操作'],files.items,f=>[f.name,h('div',{},f.display_name,h('span',{class:'muted'},f.application_name)),size(f.bytes)+' / '+f.state,btn('清理附件',async()=>{const p=await api('/admin/gateway-files/'+f.id+'/cleanup/preview','POST',{},false);ui.modal('确认清理附件',h('p',{},'将清理 '+p.filename+'，共 '+size(p.bytes)+'。引用此文件的旧会话将无法再次读取原附件；请求指标与账本保留。'),()=>api('/admin/gateway-files/'+f.id+'/cleanup','POST',{selection_hash:p.selection_hash},false),'确认清理附件');},'ghost small')]),'附件独立于会话清理，不自动删除历史文件。');
 if(files.total>20)attachments.append(h('div',{class:'pager'},'共 '+files.total+' 个附件',btn('上一批附件',async()=>{state.filePage=Math.max(1,(state.filePage||1)-1);await render();},'secondary small'),btn('下一批附件',async()=>{if((state.filePage||1)*20<files.total){state.filePage=(state.filePage||1)+1;await render();}},'secondary small')));
 return [h('div',{class:'stats'},stat('会话数量',result.total,'按当前条件筛选','book'),stat('托管已用',size(settings.used_bytes),'会话与附件共同预算','server'),stat('托管容量',size(settings.max_bytes),'手动清理','wallet'),stat('正在预留',size(settings.reserved_bytes),'派发前预留，完成后释放','bolt')),
  h('div',{class:'notice'},'客户端模式保持原样；托管模式保存历史、工具配对并自动摘要。摘要调用单独记录模型用量和费用。外部MCP连接、授权和执行由接入方负责。'),
  panel('会话筛选',controls),sessions,policies,media,attachments,
  panel('托管容量',h('div',{},h('p',{class:'subtext'},'容量不足时托管请求在派发前拒绝；请求审计的失败继续策略保持。会话清理保留财务记录，附件单独清理。'),btn('修改托管容量',()=>editor('会话与附件容量',[{key:'mib',label:'托管容量（MiB）',type:'number',required:true}],{mib:settings.max_bytes/1048576},d=>api('/admin/conversation-settings','PATCH',{max_bytes:d.mib*1048576},false)),'secondary')))
 ];
}

async function detail(ui,id){
 const {h,api,modal,btn,table,date,state,render,editor}=ui;
 const record=await api('/admin/conversations/'+id,'GET',undefined,false);
 const body=h('div',{class:'form-stack'},h('div',{class:'notice'},record.task_type+' · '+(labels[record.state]||record.state)+' · '+policyLabels[record.policy]+' · 版本 '+record.revision),
  h('p',{class:'subtext'},'账号引用：'+(record.account_reference||'尚未绑定')+'；实际账号只在内部通信中使用。'),
  h('pre',{class:'audit-content','aria-label':'会话摘要'},record.summary||'尚未需要摘要'));
 body.append(table(['轮次','类型 / 状态','操作'],record.turns,t=>[
  h('span',{},t.id),h('span',{},t.kind+' / '+t.status),h('div',{class:'actions'},
   btn('查看输入',()=>content(ui,id,t.id,'input'),'ghost small'),btn('查看输出',()=>content(ui,id,t.id,'output'),'ghost small'),
   t.request_id?btn('查看请求指标',async()=>{d.close();state.auditFilters={request_id:t.request_id};state.opsTab='requests';state.opsPage=1;await render();},'ghost small'):null)]));
 body.append(table(['绑定事件','时间'],record.binding_events,e=>[e.reason,date(e.created_at)]));
 if(['active','waiting_tools'].includes(record.state)&&!record.busy)body.append(btn('调整本会话策略',()=>editor('明确调整会话策略',[{key:'policy',label:'本会话账号策略',type:'select',options:Object.entries(policyLabels)}],{policy:record.policy},async data=>{const result=await api('/admin/conversations/'+id+'/policy','POST',{policy:data.policy,acknowledge_policy_change:true},false);d.close();return result;}),'secondary'));
 if(record.state==='active'&&!record.busy)body.append(btn('明确迁移账号',()=>modal('确认迁移会话',h('p',{},'下一次调用重新绑定账号。历史保留，供应商私有推理状态不能保证跨账号续用；不会重放之前的调用。'),()=>api('/admin/conversations/'+id+'/migrate','POST',{acknowledge_migration:true},false),'确认迁移'),'secondary'));
 const d=modal('会话详情',body);d.classList.add('audit-dialog');
}

async function content(ui,id,turn,section){
 const {h,api,modal,btn}=ui;const pre=h('pre',{class:'audit-content','aria-label':'托管轮次内容'});let cursor=0;
 const more=btn('加载更多内容',load,'secondary small');const d=modal(section==='input'?'托管输入':'托管输出',h('div',{class:'form-stack'},pre,more));d.classList.add('audit-dialog');
 async function load(){const r=await api('/admin/conversations/'+id+'/turns/'+turn+'/content?section='+section+'&cursor='+cursor,'GET',undefined,false);if(!d.open)return;pre.textContent+=r.text||'';cursor=r.next_cursor;more.hidden=cursor==null;if(!pre.textContent)pre.textContent='尚无内容或已手动清理';}
 await load();
}

async function cleanup(ui,id){
 const {h,api,modal}=ui;const preview=await api('/admin/conversations/'+id+'/cleanup/preview','POST',{},false);
 modal('确认清理托管历史',h('div',{class:'notice warning'},'将清理 '+preview.objects+' 个内容对象，共 '+size(preview.bytes)+'。会话无法按原历史续用；附件、请求指标和账本保留。'),()=>api('/admin/conversations/'+id+'/cleanup','POST',{selection_hash:preview.selection_hash},false),'确认清理历史');
}

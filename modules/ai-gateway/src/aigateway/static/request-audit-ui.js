const captureLabels={recording:'记录中',complete:'内容完整',partial:'部分缺失',disabled:'记录关闭',not_captured:'未采集',historical:'历史未采集',cleared:'正文已清理'};
const reasons={metadata_only:'模型目录读取，没有问答正文',history_not_recorded:'历史请求未保存正文',unauthenticated:'未通过账号鉴权，仅记录指标',recording_disabled:'正文记录已关闭',storage_limit:'正文容量已达上限',disk_space_low:'可用磁盘空间不足',request_not_completed:'调用未完整完成，显示已保存部分',manually_cleared:'管理员已手动清理正文',service_restarted:'服务重启，内容可能不完整',capture_write_failed:'内容写入失败',audit_storage_unavailable:'审计存储不可用',redaction_buffer_limit:'敏感内容边界无法安全分块',tool_arguments_limit:'工具参数超出记录限制'};
const stages={received:'接收',authentication:'鉴权',validation:'校验与预算',reserved:'资金冻结',credentials:'上游凭据',dispatched:'发送上游',upstream_headers:'上游响应头',first_byte:'首个数据',finished:'结束',idempotency:'关联已有请求',historical:'历史记录'};
const bytes=v=>{let n=Number(v||0);return n<1048576?(n/1024).toFixed(1)+' KiB':(n/1048576).toFixed(1)+' MiB';};

export async function requestAuditPage(ui){
 const {h,api,state,panel,btn,table,field,input,select,stat,count,money,pill,date,modal,toast,render}=ui;
 const [options,settings]=await Promise.all([api('/admin/request-audits/options','GET',undefined,false),api('/admin/request-audit-settings','GET',undefined,false)]);
 state.auditFilters??={from_time:Math.floor(Date.now()/1000)-86400};
 const query=new URLSearchParams({page:String(state.opsPage)});
 for(const [k,v] of Object.entries(state.auditFilters))if(v!==''&&v!=null)query.set(k,String(v));
 const result=await api('/admin/request-audits?'+query,'GET',undefined,false);
 const controls={};const filterGrid=h('div',{class:'form-grid audit-filters'});
 function choice(key,label,rows){const c=select(state.auditFilters[key]||'',[['','全部'],...rows]);controls[key]=c;filterGrid.append(field(label,c));}
 choice('user_id','请求用户',options.users.map(u=>[u.id,u.display_name+' · '+u.username]));
 choice('tenant_id','请求空间',options.tenants.map(t=>[t.id,t.name]));
 choice('application_id','请求应用',options.applications.map(a=>[a.id,a.name]));
 choice('key_id','请求Key',options.keys.map(k=>[k.id,k.name+' · …'+k.suffix]));
 choice('model','请求模型',options.models.map(m=>[m.alias,m.alias]));
 choice('status','调用状态',[['succeeded','已完成'],['running','处理中'],['rejected','入站拒绝'],['failed','失败'],['unknown','待对账'],['cancelled','已取消'],['released','已释放']]);
 choice('stage','调用阶段',Object.entries(stages).map(([k,v])=>[k,v]));
 for(const [key,label,type] of [['trace_id','追踪编号','text'],['request_id','财务请求编号','text'],['from_time','开始时间','datetime-local'],['to_time','结束时间','datetime-local']]){
  const v=state.auditFilters[key];const c=input(type==='datetime-local'&&v?new Date(v*1000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16):v||'',type);
  controls[key]=c;filterGrid.append(field(label,c));
 }
 filterGrid.append(h('div',{class:'actions full'},btn('应用筛选',async()=>{state.auditFilters={};for(const[k,c]of Object.entries(controls))if(c.value)state.auditFilters[k]=k.endsWith('_time')?Math.floor(new Date(c.value).getTime()/1000):c.value;state.opsPage=1;await render();},'secondary'),btn('全部时间',async()=>{state.auditFilters={};state.opsPage=1;await render();},'ghost')));
 const toolbar=h('div',{class:'actions'},btn('刷新',render,'secondary small'),btn('记录设置',()=>recordingSettings(ui,settings),'secondary small'),btn('清理正文',()=>cleanupModal(ui,options),'ghost small'));
 const rows=table(['用户 / 时间','空间 / 应用 / Key','模型 / 接口','调用 / 内容状态','Token / 费用','操作'],result.items,r=>[
  h('div',{},h('strong',{},r.display_name||r.username||'未识别用户'),h('span',{class:'muted'},r.username||'—'),h('span',{class:'muted'},date(r.created_at))),
  h('div',{},r.tenant_name||'—',h('span',{class:'muted'},r.application_name||'—'),h('span',{class:'muted'},r.key_name?r.key_name+' · …'+r.key_suffix:'—')),
  h('div',{},r.model||'未指定',h('span',{class:'muted'},r.endpoint)),
  h('div',{},r.status==='rejected'?h('span',{class:'pill red'},'入站拒绝'):pill(r.status),h('span',{class:'muted'},captureLabels[r.capture_status]||r.capture_status),h('span',{class:'muted'},r.error_code||'')),
  h('div',{},count(r.input_tokens)+' / '+count(r.output_tokens),h('span',{class:'muted'},money(r.charged_micro)),h('span',{class:'muted'},r.duration_ms+' ms')),
  h('div',{class:'actions'},btn('查看问答',()=>detail(ui,r.id),'ghost small'),r.request_id?btn('尝试记录',()=>detail(ui,r.id,'attempts'),'ghost small'):null,
   r.execution_owner&&r.status==='unknown'?btn('补充用量',()=>reconcile(ui,r.request_id),'secondary small'):null)
 ]);
 const requests=panel('用户请求记录',rows,'可按平台注册账号查看；共用Key的调用归属该Key所有者。',toolbar);
 const pager=h('div',{class:'pager'},'共 '+result.total+' 条',btn('上一页',async()=>{state.opsPage--;await render();},'secondary small'),h('span',{},state.opsPage+' / '+Math.max(1,Math.ceil(result.total/20))),btn('下一页',async()=>{state.opsPage++;await render();},'secondary small'));
 pager.querySelectorAll('button')[0].disabled=state.opsPage<=1;pager.querySelectorAll('button')[1].disabled=state.opsPage*20>=result.total;
 if(result.total>20)requests.append(pager);
 return [h('div',{class:'stats'},stat('筛选请求',count(result.total),'包含已到达网关的入站拒绝','chart'),stat('筛选消费',money(result.summary.charged_micro),'幂等重复请求不重复累计','wallet'),stat('正文存储',bytes(settings.used_bytes),'/ '+bytes(settings.max_bytes)+' · 手动清理','server'),stat('内容记录',settings.enabled?'已开启':'已关闭','部分缺失 '+count(settings.partial_contents)+' 条','book')),
  h('div',{class:'notice'+(settings.partial_contents>0||settings.used_bytes>=settings.max_bytes*0.95?' warning':'')},(settings.partial_contents?'有 '+settings.partial_contents+' 条内容记录不完整，请查看详情原因。':'')+(settings.used_bytes>=settings.max_bytes*0.95?'正文容量接近上限，请手动清理。':'')+'记录本轮问答及工具明细，正文仅运营管理员可见；容量不足时调用继续，内容标记缺失。SSH连接在到达网关前失败时没有请求记录。'),panel('筛选请求',filterGrid),requests];
}

function recordingSettings(ui,s){
 const {h,editor,api}=ui;
 return editor('请求记录设置',[{key:'enabled',label:'记录本轮问答及工具明细',type:'check'},{key:'capacity_mib',label:'正文容量（MiB）',type:'number',required:true,help:'默认1024 MiB，最高2048 MiB。达到上限后继续调用，仅停止保存新正文。'}],
  {enabled:!!s.enabled,capacity_mib:s.max_bytes/1048576},d=>api('/admin/request-audit-settings','PATCH',{enabled:d.enabled,max_bytes:d.capacity_mib*1048576},false));
}

async function detail(ui,id,initial='input'){
 const {h,api,panel,table,modal,btn,date,money,pill}=ui;
 const r=await api('/admin/request-audits/'+encodeURIComponent(id),'GET',undefined,false);
 const timeline=table(['阶段','时间'],Object.entries(r.timeline).sort((a,b)=>a[1]-b[1]),([name,t])=>[stages[name]||name,date(t)]);
 const attempts=panel('上游尝试记录',table(['序号','上游','状态','HTTP','耗时','错误'],r.attempts,a=>[a.attempt_no,a.upstream_name,pill(a.status),a.http_status||'—',a.latency_ms==null?'—':a.latency_ms+' ms',a.error_code||'—']));
 const body=h('div',{class:'form-stack'},h('div',{class:'notice'},(captureLabels[r.capture_status]||r.capture_status)+' · '+(reasons[r.capture_reason]||r.capture_reason||'本轮内容已脱敏加密保存')),
  h('div',{class:'audit-summary'},h('strong',{},(r.display_name||r.username||'未识别用户')+' · '+(r.model||'未指定模型')),
   h('span',{},'追踪编号：'+r.id),h('span',{},'财务请求：'+(r.request_id||'未进入计费')),
   h('span',{},'HTTP '+(r.http_status||'—')+' · '+r.duration_ms+' ms · 本次 '+money(r.charged_micro)),
   h('span',{},'连接来源：'+(r.source_ip||'历史未记录')+'（SSH转发时可能是回环地址）'),h('span',{},'阶段：'+(stages[r.stage]||r.stage)+' · '+(r.error_code||'无错误'))),
  panel('阶段时间线',timeline));
 const tabs=h('div',{class:'actions'}),content=h('div',{class:'form-stack'});body.append(tabs,content,attempts);
 const d=modal('请求审计详情',body);d.classList.add('audit-dialog');
 let active=0;
 async function section(name){
  const token=++active;content.replaceChildren();
  const pre=h('pre',{class:'audit-content','aria-label':{input:'完整问题',output:'完整回答',tools:'工具调用明细'}[name]}),notice=h('p',{class:'subtext'},name==='tools'?'origin为模型请求或客户端回传；客户端是否实际执行只以其回传内容为依据。':'仅展示本轮内容，历史与系统说明不重复保存。');
  const more=btn('加载更多',()=>load(),'secondary small');let cursor=0;
  content.append(notice,pre,more);
  async function load(){
   const result=await api('/admin/request-audits/'+encodeURIComponent(id)+'/content?section='+name+'&cursor='+cursor,'GET',undefined,false);
   if(token!==active||!d.open)return;
   pre.textContent+=result.text;cursor=result.next_cursor;more.hidden=cursor==null;
   if(!pre.textContent)pre.textContent=reasons[result.capture_reason]||'该部分暂无内容';
  }
  await load();
 }
 for(const[name,label]of [['input','本轮问题'],['output','模型回答'],['tools','工具过程']])tabs.append(btn(label,()=>section(name),'secondary small'));
 if(initial!=='attempts')await section(initial);
}

function reconcile(ui,id){
 return ui.editor('供应商用量对账',[{key:'input_tokens',label:'输入Token',type:'number',required:true},{key:'output_tokens',label:'输出Token',type:'number',required:true},{key:'cached_tokens',label:'缓存输入Token',type:'number'},{key:'reference',label:'供应商用量凭证',type:'textarea',required:true,full:true}],
  {input_tokens:0,output_tokens:0,cached_tokens:0,reference:''},d=>ui.api('/admin/requests/'+id+'/reconcile','POST',d,false),'确认结算');
}

function cleanupModal(ui,options){
 const {h,api,btn,field,input,select,modal,toast,render}=ui;
 const user=select('',[['','全部用户'],...options.users.map(u=>[u.id,u.display_name+' · '+u.username])]);
 const until=input(new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16),'datetime-local');
 const message=h('div',{class:'notice'},'仅清理所选范围的正文。请求索引、Token与账本保留；已有备份独立保留。每批最多100条，可重复预览下一批。');
 let selection=null;
 const execute=btn('执行清理',async()=>{const result=await api('/admin/request-audits/cleanup','POST',selection,false);d.close();toast('已清理 '+result.cleared+' 条正文，调用与账本记录保留');await render();},'');execute.disabled=true;
 const preview=btn('预览清理范围',async()=>{
  if(!until.value)throw Error('请选择截止时间');
  const body={before:Math.floor(new Date(until.value).getTime()/1000),...(user.value?{user_id:user.value}:{})};
  const p=await api('/admin/request-audits/cleanup/preview','POST',body,false);
  selection={ids:p.ids,selection_hash:p.selection_hash};execute.disabled=!p.count;
  message.textContent='本批 '+p.count+' 条正文，约 '+bytes(p.bytes)+'。执行后无法从当前库恢复正文，财务记录保留。';
 },'secondary');
 for(const c of [user,until])c.addEventListener('change',()=>{selection=null;execute.disabled=true;message.textContent='筛选条件已变化，请重新预览。';});
 const d=modal('手动清理请求正文',h('div',{class:'form-stack'},field('清理用户',user),field('清理截止时间',until),message,h('div',{class:'actions'},preview,execute)));
}

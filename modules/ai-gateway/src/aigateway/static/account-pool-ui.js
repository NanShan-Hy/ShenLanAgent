export async function accountPoolPage(ui){
 const {h,api,heading,panel,table,btn,modal,field,input,select,check,code,copy,toUnit,stat,toast,render,state,nav}=ui;
 if(!state.user.is_admin)throw Error('需要运营管理员权限');
 const base='/admin/account-pool';
 const status=await api(base+'/status','GET',undefined,false);
 const selected=new Set();
 const names={active:'可用',disabled:'已停用',error:'需要处理',unavailable:'暂不可用',pending:'等待授权',unknown:'未知'};
 function accountName(a){return a.requires_sign_in?'需要重新登录':a.disabled?'已停用':a.unavailable?'暂不可用':(names[a.status]||'未知');}
 function stepButton(label,action,ready,style='',icon=''){const b=btn(label,action,style,icon);b.disabled=!ready;return b;}
 function createConnection(){
  const tenant=select(state.tenant.id,state.user.tenants.map(t=>[t.id,t.name]));
  const [sharedEl,shared]=check('向平台其他获授权空间开放此账号池',false);
  const [consentEl,consent]=check('同意账号池在上述范围内承接项目任务',false);
  modal('创建账号池连接',h('div',{class:'form-stack'},field('账号池所属空间',tenant),sharedEl,consentEl),
   ()=>api(base+'/bootstrap','POST',{tenant_id:tenant.value,platform_shared:shared.checked,sharing_consent:consent.checked},false),'创建连接');
 }
 function authorize(){
  let flow=null,completed=false,timer=null,countdown=null,stopped=false;
  const [consentEl,consent]=check('同意新增GPT账号供当前账号池承接项目任务',false);
  const content=h('div',{class:'form-stack'},h('p',{},'使用你有权接入的GPT账号登录，每个账号分别添加。'),consentEl);
  const dialog=modal('添加GPT账号',content);
  const progress=h('div',{class:'notice',role:'status','aria-live':'polite'});
  const clock=h('p',{class:'muted'});
  function schedule(){if(dialog.isConnected&&!stopped)timer=setTimeout(poll,1500);}
  function stop(message){stopped=true;clearTimeout(timer);clearInterval(countdown);progress.textContent=message;progress.className='notice warning';}
  async function poll(){
   if(!dialog.isConnected||!flow||completed||stopped)return;
   const flowId=flow.id;
   try{
    const result=await api(base+'/authorizations/'+flowId,'GET',undefined,false);
    if(!dialog.isConnected||flow.id!==flowId)return;
    progress.textContent=result.message||'正在确认登录结果…';
    if(result.status==='ok'){
     completed=true;clearInterval(countdown);dialog.close();
     toast('GPT账号已登录并载入');
     if(status.upstream){
      try{const r=await api('/admin/upstreams/'+status.upstream.id+'/probe','POST',{},false);toast(r.status==='ok'?'模型目录已同步，请继续发布模型并生成项目Key':'账号已登录，模型目录暂未同步，请稍后手动重试',r.status!=='ok');}
      catch(e){toast('账号已登录；同步模型失败：'+e.message,true);}
     }
     await render();return;
    }
    if(result.status==='error'){stop((result.message||'账号登录失败')+' ['+(result.error_code||'unknown')+']');return;}
   }catch(e){
    if(!dialog.isConnected||flow.id!==flowId)return;
    progress.textContent=e.message+'；可重试查询或重新登录。';
    if(Date.now()>=flow.expires_at*1000){stop('登录已过期，请点击重新登录。');return;}
   }
   schedule();
  }
  async function start(){
   if(!consent.checked)throw Error('请先确认账号池任务授权范围');
   stopped=true;clearTimeout(timer);clearInterval(countdown);
   if(flow)await api(base+'/authorizations/'+flow.id,'DELETE',undefined,false).catch(()=>{});
   flow=await api(base+'/authorize','POST',{sharing_consent:true},false);stopped=false;
   const returned=input('','url',{autocomplete:'off',spellcheck:'false'});
   const manualError=h('div',{class:'modal-error',role:'alert'});
   returned.addEventListener('input',()=>{manualError.textContent='';});
   const link=h('a',{class:'btn',href:flow.authorization_url,target:'_blank',rel:'noopener noreferrer',referrerpolicy:'no-referrer'},'前往GPT登录');
   progress.textContent='等待浏览器登录并返回；回调接收后仍需确认凭据已保存和载入。';progress.className='notice';
   content.replaceChildren(h('div',{class:'notice'},'登录后自动返回并确认账号。远程访问时，请先在SSH隧道命令中加入下面的本机端口转发。'),
    code(flow.ssh_forward),h('p',{class:'muted'},'只需在Windows建立SSH隧道时加入一次。若localhost:1455页面无法访问，可使用下方手动备用。'),clock,link,progress,
    field('授权后返回的完整地址',returned,'仅在登录后页面无法打开时使用：粘贴含code和state的原始回调地址。已显示“回调已接收”时无需填写。'),manualError,
    btn('提交授权回调',async()=>{
     manualError.textContent='';
     try{
      let parsed;try{parsed=new URL(returned.value.trim());}catch{}
      if(parsed?.pathname==='/auth/callback/result'){manualError.textContent='这是自动回调后的结果页地址，无需再次提交。请查看上方最终登录状态，失败时按具体原因处理。';return;}
      await api(base+'/authorizations/'+flow.id+'/callback','POST',{redirect_url:returned.value.trim()},false);returned.value='';progress.textContent='回调已提交，正在交换登录凭据…';
     }catch(e){manualError.textContent=e.message;throw e;}
    },'secondary'),btn('重新登录',start,'secondary'));
   function tick(){const left=Math.max(0,Math.ceil(flow.expires_at-Date.now()/1000));clock.textContent='本次登录剩余 '+Math.floor(left/60)+'分'+left%60+'秒';if(!left)stop('登录已过期，请点击重新登录。');}
   tick();countdown=setInterval(tick,1000);timer=setTimeout(poll,1000);
  }
  content.append(btn('生成登录链接',start));
  dialog.addEventListener('close',()=>{
   stopped=true;clearTimeout(timer);clearInterval(countdown);
   if(flow&&!completed)api(base+'/authorizations/'+flow.id,'DELETE',undefined,false).catch(()=>{});
  },{once:true});
 }
 async function batch(action){
  if(!selected.size)throw Error('请先选择要操作的账号');
  if(selected.size>20)throw Error('每次最多选择20个账号');
  const result=await api(base+'/accounts/batch','POST',{ids:[...selected],action},false);
  const labels=new Map(status.accounts.map(a=>[a.id,a.label]));
  await render();
  modal('账号处理结果',table(['账号','结果'],result.results,r=>[labels.get(r.id)||'所选账号',r.ok?'已完成':'未完成，请检查连接或重新登录']));
 }
 async function sync(){
  if(!status.upstream)throw Error('请先创建账号池连接');
  const result=await api('/admin/upstreams/'+status.upstream.id+'/probe','POST',{},false);
  if(result.status!=='ok')throw Error('模型目录未同步成功，请检查账号是否已授权或稍后重试');
  toast('已同步 '+result.models.length+' 个模型');await render();
 }
 function publish(){
  if(!status.catalog.length)throw Error('请先同步模型目录');
  const chosen=new Set();const options=h('div',{class:'model-selection'});
  for(const model of status.catalog.slice(0,50)){
   const [label,control]=check(model,false);control.addEventListener('change',()=>control.checked?chosen.add(model):chosen.delete(model));options.append(label);
  }
  const prefix=input('gpt/'),incoming=input('','text',{inputmode:'decimal'}),cached=input('','text',{inputmode:'decimal'}),outgoing=input('','text',{inputmode:'decimal'});
  const maxOutput=input(8192,'number',{min:1,max:100000}),context=input(131072,'number',{min:2048,max:2000000});
  modal('发布模型与平台资费',h('div',{class:'form-stack'},h('div',{class:'notice'},'选择要开放的模型并填写你的平台售价。保存后启用这些路由，不会自动发送模型请求。每批最多50个。'),options,
   h('div',{class:'form-grid'},field('模型别名前缀',prefix),field('输入价格（元/百万Token）',incoming),field('缓存输入价格（元/百万Token）',cached),field('输出价格（元/百万Token）',outgoing),field('路由上下文限制',context),field('每次输出预算',maxOutput))),
   async()=>{if(!chosen.size)throw Error('请至少选择一个模型');return api(base+'/models','POST',{model_ids:[...chosen],alias_prefix:prefix.value,input_rate:toUnit(incoming.value),cache_rate:toUnit(cached.value),output_rate:toUnit(outgoing.value),max_output:Number(maxOutput.value),context_limit:Number(context.value)},false);},'发布所选模型');
 }
 function projectKey(){
  const visible=new Set(state.models.map(m=>m.alias));
  const routes=status.models.filter(m=>m.enabled&&visible.has(m.alias));
  if(!routes.length)throw Error('请先发布当前空间可使用的账号池模型');
  if(!state.apps.some(a=>a.active))throw Error('请先在应用与密钥页面创建一个启用的应用');
  const app=select(state.apps.find(a=>a.active).id,state.apps.filter(a=>a.active).map(a=>[a.id,a.name]));
  const name=input('GPT账号池项目Key');
  const model=select(routes[0].alias,routes.map(m=>[m.alias,m.alias]));
  modal('生成项目Key',h('div',{class:'form-stack'},field('接入应用',app),field('Key名称',name),field('默认模型',model),
   h('div',{class:'notice'},'Key仅授权所选模型。项目调用仍受当前空间、应用、Key配额和可用余额限制。')),
   async()=>{
    const result=await api('/keys','POST',{name:name.value,application_id:app.value,models:[model.value]});
    setTimeout(()=>{
     const secret=h('textarea',{class:'secret-box',readOnly:true,'aria-label':'平台Key'},result.key);
     const config='model_provider = "gateway"\nmodel = '+JSON.stringify(model.value)+'\nweb_search = "disabled"\n\n[model_providers.gateway]\nname = "AI Gateway"\nbase_url = '+JSON.stringify(result.base_url)+'\nwire_api = "responses"\nenv_key = "AI_GATEWAY_API_KEY"\nrequires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n';
     modal('项目接入信息',h('div',{class:'form-stack'},h('div',{class:'notice warning'},'完整Key仅显示本次，请妥善保存。首次调用前，在钱包与套餐页面确认当前空间有可用额度。'),field('平台Key',secret),btn('复制平台Key',()=>copy(result.key),'secondary'),
      field('OpenAI兼容Base URL',input(result.base_url,'text',{readonly:true})),h('h3',{},'Codex配置'),code(config),
      h('p',{class:'muted'},'将Key填写到环境变量AI_GATEWAY_API_KEY，测试平台或其他项目使用上面的Base URL、平台Key和所选模型。')));
    },0);
    return {id:result.id};
   },'创建项目Key');
 }
 const controls=[heading('GPT账号池','添加GPT账号，统一管理模型和项目接入。',h('div',{class:'actions'},btn('刷新状态',render,'secondary','refresh'),btn('添加GPT账号',authorize,'','plus'))),
  h('ol',{class:'setup-steps'},status.steps.map((s,i)=>h('li',{class:s.done?'done':''},h('span',{class:'step-number'},s.done?'✓':i+1),h('span',{},s.label)))),
  h('div',{class:'stats'},stat('账号池连接',status.online?'正常':'未连接',status.ready?'配置已就绪':'按上方步骤完成配置','server'),
   stat('GPT账号',status.accounts.length,'已启用 '+status.accounts.filter(a=>!a.disabled).length+' 个','team'),
   stat('已同步模型',status.catalog.length,'来源于当前账号池目录','book'),stat('已发布路由',status.models.filter(m=>m.enabled).length,'每个项目使用独立Key','key'))];
 if(!status.online)controls.push(h('div',{class:'notice warning'},status.configured?'账号池服务暂时无法连接，请检查运行状态后重试。':'本机账号池适配尚未连接，请完成服务连接配置。'));
 if(!status.pool)controls.push(panel('首次配置',h('div',{class:'form-stack'},h('p',{},'先选择账号池的任务共享范围，再添加账号。'),btn('创建账号池连接',createConnection,'secondary'))));
 else controls.push(panel('模型与项目接入',h('div',{class:'form-stack'},h('p',{},'当前范围：'+(status.pool.visibility==='platform'?'平台获授权项目':'指定空间的项目')),
  h('div',{class:'actions'},stepButton('同步模型目录',sync,status.online,'secondary','refresh'),stepButton('发布模型路由',publish,status.catalog.length>0,'secondary'),stepButton('生成项目Key',projectKey,status.models.some(m=>m.enabled),'','key'),btn('查看钱包与套餐',()=>nav('wallet'),'ghost')),
  table(['模型别名','状态'],status.models,m=>[h('span',{class:'mono'},m.alias),m.enabled?'已发布':'未启用']))));
 controls.push(panel('账号与调度状态',h('div',{class:'form-stack'},h('div',{class:'actions'},btn('启用所选',()=>batch('enable'),'secondary small'),btn('停用所选',()=>batch('disable'),'secondary small'),btn('续期所选',()=>batch('refresh'),'secondary small')),
  table(['选择','GPT账号','状态','恢复时间','验证记录','官方剩余额度'],status.accounts,a=>{
   const box=input('','checkbox',{'aria-label':'选择账号 '+a.label});box.addEventListener('change',()=>box.checked?selected.add(a.id):selected.delete(a.id));
   return [box,h('div',{},h('strong',{},a.label),h('span',{class:'muted'},'账号引用 '+(a.account_reference||'—'))),accountName(a),a.next_retry_at?new Date(a.next_retry_at).toLocaleString('zh-CN'):'未知',a.inference_verified?'已有成功调用':'尚未完成真实调用','未知'];
  },'还没有GPT账号，请点击添加GPT账号')),
  '新会话在可用账号间均衡分配；同会话优先保持账号。限额或授权失效时，后续请求可重选；已发送请求不自动重放。' ));
 return controls;
}

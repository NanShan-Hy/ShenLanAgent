import {createAssistant} from './ai.js';
const $ = (selector, root = document) => root.querySelector(selector);
const app = $('#app');
const state = {user:null, projects:[], project:null, page:'overview', cases:[], environments:[], suites:[], runs:[], schedules:[], notification_channels:[], health:{}, search:'', filter:'all'};
let activeReport = null;
const history={q:'',status:'',environment:'',trigger:'',since:'',until:'',page:1};
const paths = {
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  folder:'M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  layers:'m12 3 10 6-10 6L2 9z M2 14l10 6 10-6 M2 19l10 6 10-6',
  flask:'M9 3h6 M10 3v7l-6 9a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-6-9V3 M8 15h8',
  globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M3 12h18 M12 3c5 5 5 13 0 18-5-5-5-13 0-18',
  play:'m8 4 12 8-12 8z', clock:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0 M12 7v5l3 2',
  team:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M16 3a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-4',
  plus:'M12 5v14 M5 12h14', check:'m5 12 4 4L19 6', arrow:'M5 12h14 m-6-6 6 6-6 6',
  search:'M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0 M16 16l6 6', edit:'m15 4 5 5 M4 16 16 4a3 3 0 0 1 4 4L8 20l-5 1z',
  trash:'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
  close:'m6 6 12 12 M6 18 18 6', copy:'M9 9h12v12H9z M15 9V3H3v12h6',
  up:'m6 15 6-6 6 6', down:'m6 9 6 6 6-6', stop:'M5 5h14v14H5z',
  file:'M14 2H5v20h14V7z M14 2v6h6 M8 13h8 M8 17h6',
  refresh:'M20 7V3l-3 3A8 8 0 1 0 20 16 M20 3h-5',
  key:'M15 8a5 5 0 1 1-10 0 5 5 0 0 1 10 0 M11 12l10 10 M17 18l3-3',
  logout:'M10 3H4v18h6 M9 12h12 m-4-4 4 4-4 4', menu:'M4 6h16 M4 12h16 M4 18h16'
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('class','icon'); svg.setAttribute('aria-hidden','true');
  const p = document.createElementNS(svg.namespaceURI,'path'); p.setAttribute('d',paths[name]||paths.file); svg.append(p); return svg;
}
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else if (key === 'class') node.className=value;
    else if (['checked','disabled','value','readOnly','selected','multiple'].includes(key)) node[key]=value;
    else node.setAttribute(key,value);
  }
  for (const child of children.flat(Infinity)) if(child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const button = (label, action, variant='', symbol=null) => h('button',{class:'button '+variant,type:'button',onclick:action},symbol?icon(symbol):null,label);
const iconButton = (symbol,title,action) => h('button',{class:'icon-button',type:'button',title,'aria-label':title,onclick:action},icon(symbol));
const pill = status => h('span',{class:'badge '+status},({passed:'已通过',failed:'未通过',error:'执行异常',running:'执行中',queued:'排队中',cancelled:'已取消'})[status]||status);
const date = value => value ? new Date(value*1000).toLocaleString('zh-CN',{hour12:false}) : '—';
const duration = run => run.finished_at&&run.started_at ? (run.finished_at-run.started_at).toFixed(1)+' s' : '—';
const clone = value => JSON.parse(JSON.stringify(value));
const editable = () => state.project && ['admin','tester'].includes(state.project.role);
const admin = () => state.project && state.project.role==='admin';
const projectAPI = name => '/api/projects/'+state.project.id+'/'+name;
const pretty = value => JSON.stringify(value,null,2);
function toast(message,error=false) { const t=h('div',{class:'toast'+(error?' error':'')},message); $('#toasts').replaceChildren(t);setTimeout(()=>t.remove(),5500); }
async function request(url, method='GET', body) {
  const response=await fetch(url,{method,credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'TestDock'},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok){let message='请求失败';try{const data=await response.json();message=Array.isArray(data.detail)?data.detail.map(d=>d.msg).join('；'):data.detail||message;}catch{} if(response.status===401&&state.user){state.user=null;renderLogin();} throw new Error(message);}
  return response.status===204?null:response.json();
}
async function act(action) {try{return await action();}catch(error){toast(error.message,true);}}
function input(value, update, type='text', extra={}) {return h('input',{type,value:value??'',oninput:event=>update(event.target.value),...extra});}
function area(value, update, extra={}) {return h('textarea',{oninput:event=>update(event.target.value),...extra},value??'');}
function select(value, options, update) {
  return h('select',{onchange:event=>update(event.target.value)},options.map(option=>{const [v,label]=Array.isArray(option)?option:[option,option];return h('option',{value:v,selected:String(v)===String(value)},label);}));
}
let fieldNumber=0;
function field(label,control,hint=''){const id='field-'+(++fieldNumber);if(['INPUT','SELECT','TEXTAREA'].includes(control.tagName))control.id=id;return h('div',{class:'field'},h('label',{for:id},label),control,hint?h('small',{},hint):null);}
function checkbox(label,checked,update){return h('label',{class:'checkbox-row'},h('input',{type:'checkbox',checked,onchange:e=>update(e.target.checked)}),label);}
function jsonParse(text,label){try{return JSON.parse(text);}catch{throw new Error(label+'需要有效的JSON');}}
function parseValue(text){try{return JSON.parse(text);}catch{return text;}}
function closeModal(){activeReport=null;$('#modal-root').replaceChildren();}
function modal(title,body,save=null,large=false) {
  closeModal();
  const submit=button('保存',async()=>{submit.disabled=true;try{await save();closeModal();await refresh();toast('已保存');}catch(e){toast(e.message,true);}finally{submit.disabled=false;}});
  const dialog=h('div',{class:'modal'+(large?' large':''),role:'dialog','aria-modal':'true','aria-label':title},
    h('div',{class:'modal-head'},h('h2',{},title),iconButton('close','关闭',closeModal)),
    h('div',{class:'modal-body'},body),
    h('div',{class:'modal-foot'},button(save?'取消':'关闭',closeModal,'secondary'),save?submit:null));
  $('#modal-root').append(h('div',{class:'modal-backdrop'},dialog));
  setTimeout(()=>dialog.querySelector('input,select,button')?.focus(),30);
  return dialog;
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});
function empty(title,description,action=null) {return h('div',{class:'empty'},icon('layers'),h('h3',{},title),h('p',{},description),action);}
function heading(title,description,action=null){return h('div',{class:'page-heading'},h('div',{},h('div',{class:'eyebrow'},'TESTDOCK WORKSPACE'),h('h1',{},title),h('p',{},description)),action);}
function panel(title,body,action=null){return h('section',{class:'panel'},h('div',{class:'panel-header'},h('h2',{},title),action),body);}
function table(headers,rows){return h('div',{class:'table-wrap'},h('table',{},h('thead',{},h('tr',{},headers.map(x=>h('th',{},x)))),h('tbody',{},rows)));}
const cell = content=>h('td',{},content);
function brand(){return h('div',{class:'brand'},h('img',{src:'/logo.svg',alt:''}),h('div',{},'TestDock',h('small',{},'AUTOMATION WORKSPACE')));}

function renderLogin(){
  assistant.reset();closeModal();
  let username='',password='';
  const form=h('form',{class:'login-form',onsubmit:async e=>{
    e.preventDefault();submit.disabled=true;
    try{state.user=await request('/api/auth/login','POST',{username,password});await initialize();}catch(error){toast(error.message,true);}finally{submit.disabled=false;}
  }},h('h2',{},'欢迎回到工作台'),h('p',{},'登录账户，继续你的自动化测试。'));
  const submit=h('button',{class:'button',type:'submit'},'登录工作台',icon('arrow'));
  form.append(h('div',{class:'form-stack'},field('账号',input('',v=>username=v,'text',{placeholder:'请输入账号',autocomplete:'username',required:true})),
    field('密码',input('',v=>password=v,'password',{placeholder:'请输入密码',autocomplete:'current-password',required:true})),submit),
    h('p',{class:'login-note'},'账号由管理员创建 · 密码和测试凭据独立管理'));
  app.replaceChildren(h('main',{class:'login-layout'},h('section',{class:'login-art'},brand(),h('div',{},h('h1',{},'让每一次发布，',h('br'),'都有把握。'),h('p',{},'连接用例、环境与执行结果，让回归测试成为团队可靠的日常。')),h('small',{},'WEB TESTING / API TESTING / MULTI-ENVIRONMENT'),h('div',{class:'login-pattern'})),h('section',{class:'login-form-wrap'},form)));
}
const assistant=createAssistant({h,button,input,area,select,field,checkbox,modal,request,toast,act,state,pretty,date,closeModal,navigate,openReport});
const navigation=[['ai','search','AI 助手'],['overview','grid','工作台'],['cases','flask','测试用例'],['suites','layers','测试套件'],['environments','globe','环境管理'],['runs','play','执行记录'],['schedules','clock','定时任务'],['notifications','file','通知推送'],['projects','folder','项目管理'],['team','team','成员与集成'],['guide','file','接入指南']];
function renderShell(){
  const sidebar=h('aside',{class:'sidebar'},brand(),h('div',{class:'nav-label'},'WORKSPACE'));
  for(const [id,symbol,label] of navigation)sidebar.append(h('button',{class:'nav-link'+(state.page===id?' active':''),onclick:()=>{state.page=id;sidebar.classList.remove('open');renderShell();renderPage();}},icon(symbol),label,id==='cases'?h('span',{class:'nav-count'},state.cases.length):null));
  sidebar.append(h('div',{class:'sidebar-foot'},h('div',{id:'worker-status'},'执行器状态检查中'),h('div',{class:'muted'},'本机工作区 · TestDock 1.3')));
  const projectSelect=select(state.project?.id||'',state.projects.length?state.projects.map(p=>[p.id,p.name]):[['','暂无项目']],async value=>{state.project=state.projects.find(p=>p.id===Number(value));localStorage.setItem('testdock-project',value);await refresh();});
  projectSelect.className='project-select';projectSelect.setAttribute('aria-label','当前项目');
  const top=h('header',{class:'topbar'},h('div',{class:'top-left'},h('button',{class:'mobile-menu icon-button',onclick:()=>sidebar.classList.toggle('open'),'aria-label':'展开菜单'},icon('menu')),h('span',{},'工作空间'),h('span',{},'/'),h('strong',{},navigation.find(n=>n[0]===state.page)?.[2])),
    h('div',{class:'top-right'},button('AI 助手',()=>act(()=>assistant.open()),'secondary small ai-quick','search'),projectSelect,h('button',{class:'user-button',onclick:accountModal,title:'账户设置'},h('span',{class:'avatar'},state.user.display_name.slice(0,1).toUpperCase()),h('span',{class:'user-name'},state.user.display_name))));
  app.replaceChildren(h('div',{class:'shell'},sidebar,h('div',{class:'main'},top,h('main',{class:'content',id:'content'}))));
  showHealth();
}
function showHealth(){const target=$('#worker-status');if(target)target.replaceChildren(h('span',{class:'online-dot'+(state.health.worker_online?'':' off')}),state.health.worker_online?'执行器在线':'执行器未连接');}
async function initialize(){
  state.projects=await request('/api/projects');
  state.project=state.projects.find(p=>p.id===Number(localStorage.getItem('testdock-project')))||state.projects[0]||null;
  await refresh();await act(openLinkedReport);
}
async function refresh(){
  if(!state.user)return;
  if(state.project){
    const id=state.project.id;
    const values=await Promise.all(['cases','environments','suites','runs','schedules','notification_channels'].map(name=>request('/api/projects/'+id+'/'+(name==='notification_channels'?'notification-channels':name))));
    if(state.project?.id!==id)return;
    ['cases','environments','suites','runs','schedules','notification_channels'].forEach((key,index)=>state[key]=values[index]);
  }else for(const key of ['cases','environments','suites','runs','schedules','notification_channels'])state[key]=[];
  state.health=await request('/api/health');renderShell();await renderPage();
}
async function renderPage(){
  const content=$('#content');if(!content)return;
  if(!state.project&&!['projects','ai'].includes(state.page)){content.replaceChildren(heading('欢迎使用 TestDock','建立第一个项目，开始组织团队测试。'),empty('工作区准备就绪','一个项目集中管理用例、环境、成员和回归记录。',state.user.is_admin?button('创建项目',()=>projectModal(),'','plus'):null));return;}
  const renderers={ai:assistant.page,overview:overviewPage,cases:casesPage,suites:suitesPage,environments:environmentsPage,runs:runsPage,schedules:schedulesPage,notifications:notificationsPage,projects:projectsPage,team:teamPage,guide:guidePage};
  try{const result=await renderers[state.page]();if($('#content')===content)content.replaceChildren(...(Array.isArray(result)?result:[result]));}catch(e){toast(e.message,true);}
}
function stat(label,value,note,symbol,color=''){return h('div',{class:'stat'},h('div',{},h('div',{class:'stat-label'},label),h('div',{class:'stat-value'},value),h('div',{class:'stat-note'},note)),h('div',{class:'stat-icon '+color},icon(symbol)));}
function runTable(runs){
  if(!runs.length)return empty('还没有执行记录','配置用例与环境后，启动第一次回归测试。');
  return table(['执行任务','环境','状态','触发方式','耗时','执行时间',''],runs.map(run=>h('tr',{},
    cell(h('div',{},h('strong',{},run.name),h('div',{class:'subtext mono'},'#'+run.id.slice(0,8)))),
    cell(run.environment_name),cell(pill(run.status)),cell(({manual:'手动执行',schedule:'定时任务',ci:'CI 调用'})[run.trigger]||run.trigger),
    cell(duration(run)),cell(date(run.created_at)),cell(button('查看报告',()=>openReport(run.id),'ghost small','arrow')))));
}
function overviewPage(){
  const ended=state.runs.filter(r=>['passed','failed','error'].includes(r.status));
  const passed=ended.filter(r=>r.status==='passed').length;
  const rate=ended.length?Math.round(passed/ended.length*100):null;
  const hero=h('section',{class:'hero'},h('div',{},h('h2',{},'把测试连接成可靠的交付流程'),h('p',{},'在不同环境运行同一套用例，统一查看 Web 与接口测试结果，让每一次回归都清晰可追溯。'),editable()?button('启动测试',()=>runModal(),'','play'):button('查看执行记录',()=>navigate('runs'),'secondary','arrow')),
    h('div',{class:'hero-graphic','aria-hidden':'true'},h('div',{class:'graphic-card'}),h('div',{class:'graphic-check'},icon('check'))));
  return [heading('测试工作台','你好，'+state.user.display_name+'。这里是 '+state.project.name+' 的质量概览。',button('刷新数据',()=>act(refresh),'secondary','refresh')),hero,
    h('div',{class:'stats-grid'},stat('测试用例',state.cases.length,'Web 与 API 用例','flask'),stat('最近执行通过率',rate===null?'—':rate+'%','最近 '+ended.length+' 次已结束执行','check','green'),stat('执行任务',state.runs.length,'排队 / 运行中 '+state.runs.filter(r=>['queued','running'].includes(r.status)).length+' 个','play','purple'),stat('被测环境',state.environments.length,'独立配置 · 运行时快照','globe','orange')),
    panel('最近执行',runTable(state.runs.slice(0,6)),button('查看全部',()=>navigate('runs'),'ghost small','arrow')),
    h('div',{class:'split-grid'},panel('用例资产',h('div',{class:'panel-body'},h('div',{class:'row'},h('span',{class:'tag web'},'WEB'),h('span',{class:'muted'},state.cases.filter(c=>c.kind==='web').length+' 个页面与混合用例')),h('div',{class:'progress-bar'},h('span',{style:'width:'+(state.cases.length?state.cases.filter(c=>c.kind==='web').length/state.cases.length*100:0)+'%'})),h('div',{class:'row'},h('span',{class:'tag'},'API'),h('span',{class:'muted'},state.cases.filter(c=>c.kind==='api').length+' 个接口用例')),h('p',{class:'footnote'},'套件按选定顺序执行；每个用例拥有独立会话和变量。'))),
      panel('环境一览',h('div',{class:'panel-body'},state.environments.length?state.environments.map(env=>h('div',{class:'environment-line'},h('div',{},h('span',{class:'env-dot'}),env.name),h('span',{class:'muted'},Object.keys(env.variables).length+env.secret_keys.length+' 个变量'))):h('p',{class:'muted'},'添加开发、测试或预发环境。')),button('管理',()=>navigate('environments'),'ghost small')))];
}
function navigate(page){state.page=page;renderShell();renderPage();}
function casesPage(){
  const rowsContainer=h('div');
  const draw=()=>{
    const cases=state.cases.filter(c=>(state.filter==='all'||c.kind===state.filter)&&(c.name+' '+c.tags.join(' ')).toLowerCase().includes(state.search.toLowerCase()));
    rowsContainer.replaceChildren(panel('用例列表 · '+cases.length,cases.length?table(['用例名称','类型','步骤','版本','最近更新','操作'],cases.map(c=>h('tr',{},cell(h('div',{},h('strong',{},c.name),h('div',{class:'subtext'},c.description||c.tags.join(' · ')||'未添加描述'))),cell(h('span',{class:'tag '+c.kind},c.kind.toUpperCase())),cell(c.steps.length+' 个步骤'),cell('v'+c.revision),cell(date(c.updated_at)),cell(h('div',{class:'actions'},editable()?button('运行',()=>runModal([c.id]),'ghost small','play'):null,editable()?iconButton('edit','编辑用例',()=>caseModal(c)):null,editable()?iconButton('copy','复制用例',()=>caseModal({...clone(c),id:null,name:c.name+' · 副本'})):null,editable()?iconButton('trash','删除用例',()=>remove('cases',c.id,c.name)):null))))):empty('开始编排测试用例','通过步骤表单配置页面操作、HTTP 请求和断言。',editable()?button('新建用例',()=>caseModal(),'','plus'):null)));
  };
  const filters=h('div',{class:'filters'});
  for(const [value,label] of [['all','全部用例'],['web','Web UI'],['api','接口测试']])filters.append(h('button',{class:'filter'+(state.filter===value?' selected':'')},label));
  // Explicit target handling keeps keyboard and pointer activation equivalent.
  [...filters.children].forEach((node,index)=>node.onclick=()=>{state.filter=['all','web','api'][index];[...filters.children].forEach(n=>n.classList.toggle('selected',n===node));draw();});
  draw();
  return [heading('测试用例','把操作和断言编排成可重复执行的测试。',editable()?h('div',{class:'actions'},button('导入',importModal,'secondary','file'),button('导出',exportAssets,'secondary','file'),button('新建用例',()=>caseModal(),'','plus')):null),
    h('div',{class:'toolbar'},filters,h('div',{class:'search'},icon('search'),input(state.search,v=>{state.search=v;draw();},'search',{placeholder:'搜索用例名称或标签','aria-label':'搜索用例'}))),rowsContainer];
}
const actionOptions=[['goto','打开页面'],['click','点击元素'],['fill','输入内容'],['select','选择选项'],['check','勾选 / 取消'],['press','键盘按键'],['assert_text','检查文本'],['assert_visible','检查可见'],['assert_url','检查 URL'],['wait_for','等待元素'],['screenshot','保存截图'],['http','HTTP 请求']];
function newStep(action='goto'){return {action,name:'',locator_type:'css',locator:'',value:'',url:action==='goto'?'/':'',method:'GET',headers:{},query:{},body:null,body_type:'json',assertions:action==='http'?[{source:'status',expression:'',operator:'eq',expected:200}]:[],extract:[],timeout:15};}
function caseModal(existing=null){
  const draft=existing?clone(existing):{name:'',description:'',tags:[],steps:[newStep()]};
  let tags=draft.tags.join(', ');
  const stepsArea=h('div');
  const move=(i,delta)=>{const target=i+delta;if(target>=0&&target<draft.steps.length){[draft.steps[i],draft.steps[target]]=[draft.steps[target],draft.steps[i]];drawSteps();}};
  const drawSteps=()=>{
    stepsArea.replaceChildren();
    draft.steps.forEach((step,index)=>{
      const card=h('section',{class:'step-card'});
      const action=select(step.action,actionOptions,value=>{step.action=value;if(value==='http'&&!step.assertions.length)step.assertions=[{source:'status',expression:'',operator:'eq',expected:200}];drawSteps();});
      card.append(h('div',{class:'step-head'},h('span',{class:'step-number'},index+1),field('操作',action),field('步骤名称',input(step.name,v=>step.name=v,'text',{placeholder:'可选，描述这一步'})),
        h('div',{class:'actions'},iconButton('up','上移',()=>move(index,-1)),iconButton('down','下移',()=>move(index,1)),iconButton('copy','复制步骤',()=>{draft.steps.splice(index+1,0,clone(step));drawSteps();}),iconButton('trash','移除步骤',()=>{draft.steps.splice(index,1);drawSteps();}))));
      const grid=h('div',{class:'form-grid'});
      if(step.action==='http'){
        grid.append(field('请求方法',select(step.method,['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'],v=>step.method=v)),
          field('URL / 相对路径',input(step.url,v=>step.url=v,'text',{placeholder:'/api/orders 或完整 URL'}),'相对路径使用当前环境 API 基础地址'));
        if(step._headers===undefined)step._headers=pretty(step.headers);
        if(step._query===undefined)step._query=pretty(step.query);
        if(step._body===undefined)step._body=step.body===null?'':typeof step.body==='string'?step.body:pretty(step.body);
        grid.append(field('Headers · JSON',area(step._headers,v=>step._headers=v,{class:'mono'})),field('Query · JSON',area(step._query,v=>step._query=v,{class:'mono'})),
          field('请求体类型',select(step.body_type,['json','form','text'],v=>step.body_type=v)),field('请求体',area(step._body,v=>step._body=v,{class:'mono'})));
      }else{
        if(step.action==='goto')grid.append(field('URL / 相对路径',input(step.url,v=>step.url=v,'text',{placeholder:'/login'}),'相对路径使用当前环境 Web 基础地址'));
        if(!['goto','screenshot','assert_url'].includes(step.action))grid.append(field('定位方式',select(step.locator_type,[['css','CSS / Playwright 选择器'],['text','可见文本'],['testid','data-testid'],['label','表单标签'],['placeholder','占位文字']],v=>step.locator_type=v)),field('定位值',input(step.locator,v=>step.locator=v,'text',{placeholder:'#submit 或按钮文字'})));
        if(['fill','select','check','press','assert_text','assert_url','wait_for'].includes(step.action))grid.append(field(({fill:'输入内容',select:'选项 value',check:'true / false',press:'按键，例如 Enter',assert_text:'应包含的文本',assert_url:'URL 应包含',wait_for:'visible / hidden / attached / detached'})[step.action],input(step.value,v=>step.value=v),'支持 {{变量名}} 引用当前环境或前面提取的值'));
      }
      grid.append(field('步骤超时（秒）',input(step.timeout,v=>step.timeout=Number(v),'number',{min:1,max:120})));
      card.append(grid);
      if(step.action==='http'){
        const assertions=h('div',{class:'subsection'},h('div',{class:'subsection-title'},'响应断言',button('添加断言',()=>{step.assertions.push({source:'json',expression:'',operator:'eq',expected:''});drawSteps();},'ghost small','plus')));
        step.assertions.forEach((a,i)=>{
          const value=a._expected===undefined?(typeof a.expected==='string'?a.expected:JSON.stringify(a.expected)):a._expected;
          assertions.append(h('div',{class:'assert-row'},select(a.source,[['status','状态码'],['json','JSON'],['text','文本'],['header','响应头'],['duration','耗时 ms']],v=>a.source=v),
            input(a.expression,v=>a.expression=v,'text',{placeholder:'JMESPath，例如 data.id'}),select(a.operator,[['eq','等于'],['ne','不等于'],['contains','包含'],['exists','存在'],['lt','小于'],['gt','大于']],v=>a.operator=v),
            input(value,v=>a._expected=v,'text',{placeholder:'预期值 / {{变量}}'}),iconButton('trash','移除断言',()=>{step.assertions.splice(i,1);drawSteps();})));
        });
        const extracts=h('div',{class:'subsection'},h('div',{class:'subsection-title'},'响应提取',button('添加提取',()=>{step.extract.push({name:'',source:'json',expression:'',secret:false});drawSteps();},'ghost small','plus')));
        step.extract.forEach((x,i)=>extracts.append(h('div',{class:'assert-row'},select(x.source,[['json','JSON'],['header','响应头'],['text','全文']],v=>x.source=v),input(x.expression,v=>x.expression=v,'text',{placeholder:'表达式，例如 data.token'}),input(x.name,v=>x.name=v,'text',{placeholder:'变量名'}),checkbox('敏感值',x.secret,v=>x.secret=v),iconButton('trash','移除提取',()=>{step.extract.splice(i,1);drawSteps();}))));
        card.append(assertions,extracts);
      }
      stepsArea.append(card);
    });
    if(!draft.steps.length)stepsArea.append(empty('添加第一个步骤','从页面操作或 HTTP 请求开始。'));
  };
  drawSteps();
  modal(existing?.id?'编辑用例':'新建用例',h('div',{},h('div',{class:'form-grid'},field('用例名称',input(draft.name,v=>draft.name=v)),field('标签（英文逗号分隔）',input(tags,v=>tags=v)),field('描述',area(draft.description,v=>draft.description=v))),
    h('div',{class:'step-editor-title'},h('h3',{},'测试步骤'),h('div',{class:'row'},button('添加 Web 步骤',()=>{draft.steps.push(newStep());drawSteps();},'secondary small','plus'),button('添加接口请求',()=>{draft.steps.push(newStep('http'));drawSteps();},'secondary small','plus'))),
    h('div',{class:'notice'},'步骤按顺序运行。变量来自所选环境；响应提取仅在当前用例内传递。'),stepsArea),
    async()=>{
      const cleanSteps=draft.steps.map(step=>{
        const result=Object.fromEntries(Object.entries(step).filter(([k])=>!k.startsWith('_')));
        if(step.action==='http'){result.headers=jsonParse(step._headers||'{}','Headers');result.query=jsonParse(step._query||'{}','Query');result.body=step._body?step.body_type==='text'?step._body:jsonParse(step._body,'请求体'):null;}
        result.assertions=step.assertions.map(a=>({source:a.source,expression:a.expression,operator:a.operator,expected:a._expected===undefined?a.expected:parseValue(a._expected)}));
        return result;
      });
      await request(projectAPI('cases')+(existing?.id?'/'+existing.id:''),existing?.id?'PUT':'POST',{name:draft.name,description:draft.description,tags:tags.split(',').map(t=>t.trim()).filter(Boolean),steps:cleanSteps});
    },true);
}
function environmentsPage(){
  return [heading('环境管理','同一套用例，独立的地址、变量和凭据。',editable()?button('添加环境',()=>environmentModal(),'','plus'):null),
    h('div',{class:'notice'},'提交执行时会保存环境快照。后续修改配置不会改变已经排队或运行的任务。敏感变量保存后只显示名称。'),
    state.environments.length?h('div',{class:'cards-grid'},state.environments.map(env=>h('section',{class:'resource-card'},h('div',{class:'row'},h('div',{class:'stat-icon'},icon('globe')),h('span',{class:'flex-spacer'}),h('span',{class:'badge'},env.secret_keys.length+' 个敏感变量')),
      h('h3',{},env.name),h('div',{class:'url'},'WEB  '+(env.web_base_url||'未配置')),h('div',{class:'url'},'API  '+(env.api_base_url||'未配置')),
      h('div',{class:'card-foot'},h('span',{},Object.keys(env.variables).length+' 个普通变量'),editable()?h('div',{class:'actions'},button('编辑配置',()=>environmentModal(env),'ghost small'),iconButton('trash','删除环境',()=>remove('environments',env.id,env.name))):h('span',{},'只读'))))):empty('添加被测环境','例如开发、测试、预发，各自保存地址与变量。')];
}
function environmentModal(existing=null){
  const data=existing?clone(existing):{name:'',web_base_url:'',api_base_url:'',variables:{},secret_keys:[]};
  let variables=pretty(data.variables),hidden='{}';const removed=new Set();
  modal(existing?'编辑环境':'添加环境',h('div',{class:'form-stack'},field('环境名称',input(data.name,v=>data.name=v)),field('Web 基础地址',input(data.web_base_url,v=>data.web_base_url=v,'url',{placeholder:'https://test.example.com'})),field('API 基础地址',input(data.api_base_url,v=>data.api_base_url=v,'url',{placeholder:'https://api-test.example.com'})),
    field('普通变量 · JSON',area(variables,v=>variables=v,{class:'mono'}),'例如 {"username":"tester","expected_title":"测试环境"}'),
    field('新增 / 更新敏感变量 · JSON',area(hidden,v=>hidden=v,{class:'mono',autocomplete:'off'}),'例如 {"password":"真实值"}。留空对象会保留已存值。'),
    data.secret_keys.length?h('div',{},h('p',{class:'footnote'},'勾选要删除的已存敏感变量'),data.secret_keys.map(key=>checkbox(key,false,on=>on?removed.add(key):removed.delete(key)))):null),
    ()=>request(projectAPI('environments')+(existing?'/'+existing.id:''),existing?'PUT':'POST',{name:data.name,web_base_url:data.web_base_url,api_base_url:data.api_base_url,variables:jsonParse(variables,'普通变量'),secrets:jsonParse(hidden||'{}','敏感变量'),remove_secrets:[...removed]}));
}
function suitesPage(){
  const rows=state.suites.map(s=>h('tr',{},
    cell(h('strong',{},s.name)),cell(s.case_ids.length+' 个'),cell(s.description||'—'),
    cell(h('div',{class:'actions'},editable()?button('运行套件',()=>runModal(null,s.id),'ghost small','play'):null,
      editable()?iconButton('edit','编辑套件',()=>suiteModal(s)):null,
      editable()?iconButton('trash','删除套件',()=>remove('suites',s.id,s.name)):null))));
  return [heading('测试套件','按业务流程组织用例，复用一套回归计划。',editable()?button('新建套件',()=>suiteModal(),'','plus'):null),
    panel('套件列表',rows.length?table(['套件名称','用例数量','说明','操作'],rows):empty('创建你的回归套件','将多个用例按固定顺序组织起来。'))];
}
function suiteModal(existing=null){
  const data=existing?clone(existing):{name:'',description:'',case_ids:[]};
  const chosen=h('div');
  const draw=()=>chosen.replaceChildren(...data.case_ids.map((id,index)=>{
    const item=state.cases.find(c=>c.id===id);
    return h('div',{class:'environment-line'},h('span',{},(index+1)+'. '+(item?.name||'已删除用例')),h('div',{class:'actions'},iconButton('up','前移',()=>{if(index){[data.case_ids[index-1],data.case_ids[index]]=[data.case_ids[index],data.case_ids[index-1]];draw();}}),iconButton('down','后移',()=>{if(index<data.case_ids.length-1){[data.case_ids[index+1],data.case_ids[index]]=[data.case_ids[index],data.case_ids[index+1]];draw();}})));
  }));draw();
  modal(existing?'编辑套件':'新建套件',h('div',{class:'form-stack'},field('套件名称',input(data.name,v=>data.name=v)),field('说明',area(data.description,v=>data.description=v)),
    field('选择用例',h('div',{class:'checklist'},state.cases.length?state.cases.map(c=>checkbox(c.name,data.case_ids.includes(c.id),on=>{data.case_ids=on?[...data.case_ids,c.id]:data.case_ids.filter(id=>id!==c.id);draw();})):h('p',{class:'footnote'},'请先创建测试用例'))),field('执行顺序',chosen)),
    ()=>request(projectAPI('suites')+(existing?'/'+existing.id:''),existing?'PUT':'POST',data.id?{name:data.name,description:data.description,case_ids:data.case_ids}:data));
}
function runModal(caseIds=null,suiteId=null){
  if(!editable()){toast('只读成员无法执行测试',true);return;}
  if(!state.environments.length){toast('请先创建被测环境',true);return;}
  if(!caseIds&&!suiteId&&!state.suites.length){toast('请先创建套件，或从用例列表运行单个用例',true);return;}
  let environment=state.environments[0].id,suite=suiteId||state.suites[0]?.id;const extras=new Set();
  const content=h('div',{class:'form-stack'},field('被测环境',select(environment,state.environments.map(e=>[e.id,e.name]),v=>environment=Number(v))),
    caseIds?h('div',{class:'notice'},'将运行 '+caseIds.length+' 个用例，保存当前版本和环境快照。'):field('测试套件',select(suite,state.suites.map(s=>[s.id,s.name]),v=>suite=Number(v))),
    field('同时执行其他环境',h('div',{class:'checklist'},state.environments.map(e=>checkbox(e.name,false,on=>on?extras.add(e.id):extras.delete(e.id))))),h('p',{class:'footnote'},'每个环境保存独立快照并生成报告，任务依次执行。'));
  const dialog=modal('启动测试',content,async()=>{const ids=[...new Set([environment,...extras])];const payload={suite_id:caseIds?null:suite,case_ids:caseIds||[]};const data=ids.length===1?await request(projectAPI('runs'),'POST',{...payload,environment_id:environment}):await request(projectAPI('runs/matrix'),'POST',{...payload,environment_ids:ids});state.page='runs';history.page=1;setTimeout(()=>openReport(data.id||data.runs[0].id),100);});
  $('.modal-foot .button:not(.secondary)',dialog).replaceChildren(icon('play'),'提交执行');
}
async function runsPage(){
  const params=new URLSearchParams({q:history.q,status:history.status,environment:history.environment,trigger:history.trigger,page:history.page,page_size:20});
  for(const key of ['since','until'])if(history[key])params.set(key,new Date(history[key]).getTime()/1000);
  const result=await request(projectAPI('runs/search')+'?'+params);
  const apply=()=>{history.page=1;act(renderPage);};
  const filters=h('div',{class:'history-filters form-grid'},
    field('任务名称 / 编号',input(history.q,v=>history.q=v,'search')),
    field('执行状态',select(history.status,[['','全部状态'],['passed','已通过'],['failed','未通过'],['error','异常'],['cancelled','已取消'],['queued','排队'],['running','执行中']],v=>history.status=v)),
    field('环境筛选',input(history.environment,v=>history.environment=v,'text',{placeholder:'环境名称，留空为全部'})),
    field('触发方式',select(history.trigger,[['','全部方式'],['manual','手动'],['schedule','定时'],['ci','CI']],v=>history.trigger=v)),
    field('开始时间',input(history.since,v=>history.since=v,'datetime-local')),
    field('结束时间',input(history.until,v=>history.until=v,'datetime-local')),
    h('div',{class:'actions'},button('查询',apply,'','search'),button('重置',()=>{Object.assign(history,{q:'',status:'',environment:'',trigger:'',since:'',until:'',page:1});act(renderPage);},'secondary')));
  const pages=Math.max(1,Math.ceil(result.total/20));
  const prev=button('上一页',()=>{history.page--;act(renderPage);},'secondary small');prev.disabled=history.page<=1;
  const next=button('下一页',()=>{history.page++;act(renderPage);},'secondary small');next.disabled=history.page>=pages;
  return [heading('执行记录','筛选全部历史运行，查看逐步报告。',editable()?button('启动测试',()=>runModal(),'','play'):null),
    panel('查询条件',h('div',{class:'panel-body'},filters)),
    panel('执行历史 · '+result.total+' 条',runTable(result.items),button('刷新',()=>act(renderPage),'ghost small','refresh')),
    h('div',{class:'toolbar'},h('span',{class:'muted'},'符合条件：通过 '+(result.counts.passed||0)+' · 失败 '+(result.counts.failed||0)+' · 异常 '+(result.counts.error||0)),h('div',{class:'actions'},prev,h('span',{},history.page+' / '+pages),next))];
}
async function openReport(id){
  try{
    const holder=h('div',{class:'loading'},'正在加载报告…');modal('执行报告',holder,null,true);activeReport=id;
    await fillReport(id,holder);
  }catch(e){toast(e.message,true);}
}
async function fillReport(id,holder){
  const run=await request('/api/runs/'+id);if(activeReport!==id||!holder.isConnected)return;
  const counts=run.summary||{};
  const top=h('div',{class:'row report-actions'},button('AI 分析',()=>act(()=>assistant.open({runId:id})),'secondary small'),pill(run.status),h('strong',{},run.name),h('span',{class:'flex-spacer'}),['queued','running'].includes(run.status)&&editable()?button('取消执行',()=>act(async()=>{await request('/api/runs/'+id+'/cancel','POST');await fillReport(id,holder);}), 'danger small','stop'):null,
    ...(!['queued','running'].includes(run.status)&&editable()?[button('重跑原快照',()=>rerunReport(id,false),'secondary small','refresh'),...(run.status!=='passed'?[button('复现未通过快照',()=>rerunReport(id,true),'secondary small'),button('按最新配置重跑未通过',()=>rerunReport(id,true,true),'secondary small')]:[])]:[]),!['queued','running'].includes(run.status)?h('a',{href:'/api/runs/'+id+'/junit.xml',class:'button secondary small',download:'junit.xml'},'JUnit XML'):null);
  holder.className='';
  holder.replaceChildren(top,h('p',{class:'footnote'},'环境：'+run.environment_name+' · '+date(run.created_at)+' · 执行编号 '+id),
    run.error?h('div',{class:'notice warning'},run.error):null,
    run.status==='running'&&run.progress?h('div',{class:'notice'},h('span',{class:'spinner'}),' '+run.progress.case+' · 步骤 '+run.progress.step+'/'+run.progress.total_steps+' '+run.progress.name):null,
    h('div',{class:'report-summary'},stat('通过',counts.passed||0,'用例','check','green'),stat('失败',counts.failed||0,'断言未通过','flask','orange'),stat('异常',counts.error||0,'执行错误','file','purple'),stat('耗时',duration(run),'本次执行','clock')),
    ...run.results.map(result=>h('details',{class:'result-case',open:result.status!=='passed'?'':null},h('summary',{},pill(result.status),result.name,h('span',{class:'duration'},result.duration+' s')),
      ...result.steps.map(step=>h('details',{class:'result-step',open:step.status!=='passed'?'':null},h('summary',{},h('span',{},step.index+'. '+step.name),h('span',{},pill(step.status),' '+step.duration+' s')),
        step.error?h('pre',{class:'error-text'},step.error):null,
        step.request?h('div',{},h('h4',{},'请求'),h('pre',{},pretty(step.request)),h('h4',{},'响应'),h('pre',{},pretty(step.response))):null,
        step.assertions?.length?h('pre',{},pretty(step.assertions)):null,
        step.screenshot?h('a',{href:artifactURL(id,step.screenshot),target:'_blank',rel:'noopener'},h('img',{class:'report-image',src:artifactURL(id,step.screenshot),alt:'步骤截图'})):null)))),
    run.artifacts.length?panel('执行附件',h('div',{class:'panel-body form-stack'},run.artifacts.map(name=>h('a',{href:artifactURL(id,name),target:'_blank',rel:'noopener'},name)))):null,
    h('p',{class:'footnote'},'用例快照：'+run.case_revisions.map(c=>c.name+' v'+c.revision).join(' · ')));
  if(['queued','running'].includes(run.status))setTimeout(()=>{if(activeReport===id)act(()=>fillReport(id,holder));},1500);
}
function artifactURL(id,name){return '/api/runs/'+id+'/artifacts/'+name.split('/').map(encodeURIComponent).join('/');}
async function schedulesPage(){
  const recent=await request(projectAPI('schedule-batches'));
  return [heading('定时任务','每天按时执行多环境回归，全部环境结束后汇总推送。',editable()?button('新建定时任务',()=>scheduleModal(),'','plus'):null),
    h('div',{class:'notice'},'默认北京时间09:00，可选择每日、工作日或自定义Cron。已有批次排队或运行时跳过重叠触发；超过一分钟的过期触发不补跑。'),
    panel('任务计划',state.schedules.length?table(['任务名称','Cron / 时区','套件 / 环境','下次触发','状态','操作'],state.schedules.map(s=>h('tr',{},cell(h('div',{},h('strong',{},s.name),s.last_error?h('div',{class:'subtext error-text'},s.last_error):null)),cell(h('div',{},h('span',{class:'mono'},s.cron),h('div',{class:'subtext'},s.timezone))),cell(h('div',{},state.suites.find(x=>x.id===s.suite_id)?.name,h('div',{class:'subtext'},(s.environment_ids||[s.environment_id]).map(id=>state.environments.find(x=>x.id===id)?.name||id).join('、')))),cell(s.enabled?date(s.next_run_at):'—'),cell(h('span',{class:'badge '+(s.enabled?'passed':'cancelled')},s.enabled?'已启用':'已暂停')),cell(editable()?h('div',{class:'actions'},iconButton('edit','编辑计划',()=>scheduleModal(s)),button(s.enabled?'暂停':'启用',()=>act(async()=>{await saveSchedule({...s,enabled:!s.enabled});await refresh();}),'ghost small'),iconButton('trash','删除定时任务',()=>remove('schedules',s.id,s.name))):'只读')))):empty('还没有定时任务','先创建套件与环境，再安排每日回归。')),
    panel('最近执行批次',recent.items.length?h('div',{class:'panel-body form-stack'},...recent.items.map(b=>h('div',{class:'row wrap'},h('strong',{},b.name),h('span',{},date(b.scheduled_for)),h('span',{class:'badge'},b.status==='skipped'?'已跳过':b.status==='running'?'执行中':statusLabel(b.status)),h('span',{class:'muted'},b.reason),button('查看批次',()=>openBatch(b.id),'ghost small'))),recent.total>20?h('p',{},'这里只显示最近20个批次；更早记录可按编号打开。'):null):empty('暂无批次','计划启用后将在下次时间触发。'))];
}
function saveSchedule(d){return request(projectAPI('schedules')+(d.id?'/'+d.id:''),d.id?'PUT':'POST',{name:d.name,suite_id:d.suite_id,environment_ids:d.environment_ids||[d.environment_id],cron:d.cron,timezone:d.timezone,enabled:d.enabled,notification_channel_ids:d.notification_channel_ids??null,notify_mode:d.notify_mode??null});}
function scheduleModal(existing=null){
  if(!state.suites.length||!state.environments.length){toast('请先创建套件与环境',true);return;}
  const d=existing?clone(existing):{name:'',suite_id:state.suites[0].id,environment_ids:[state.environments[0].id],cron:'0 9 * * *',timezone:'Asia/Shanghai',enabled:false,notification_channel_ids:null,notify_mode:null};
  d.environment_ids=d.environment_ids||[d.environment_id];const chosen=new Set(d.environment_ids);let selected=new Set(d.notification_channel_ids||[]),inherit=d.notification_channel_ids===null;
  const m=d.cron.match(/^(\d+) (\d+) \* \* (\*|1-5)$/);let frequency=m?(m[3]==='*'?'daily':'weekdays'):'custom',clock=m?m[2].padStart(2,'0')+':'+m[1].padStart(2,'0'):'09:00';
  const cronBox=input(d.cron,v=>{d.cron=v;frequency='custom';frequencyBox.value='custom';});
  const updateCron=()=>{if(frequency!=='custom'){const [hh,mm]=clock.split(':').map(Number);d.cron=mm+' '+hh+' * * '+(frequency==='daily'?'*':'1-5');cronBox.value=d.cron;}};
  const frequencyBox=select(frequency,[['daily','每天'],['weekdays','工作日'],['custom','自定义Cron']],v=>{frequency=v;updateCron();});
  const preview=h('div',{class:'notice'},'可预览未来五次执行时间。');
  const inheritControl=checkbox('使用项目默认通知规则',inherit,v=>inherit=v);
  const choices=h('div',{class:'form-stack'},...state.notification_channels.map(c=>checkbox(c.name+' · '+(c.enabled?'已启用':'未启用'),selected.has(c.id),v=>{v?selected.add(c.id):selected.delete(c.id);inherit=false;$('input',inheritControl).checked=false;})));
  modal(existing?'编辑定时任务':'新建定时任务',h('div',{class:'form-stack'},field('任务名称',input(d.name,v=>d.name=v)),field('套件',select(d.suite_id,state.suites.map(s=>[s.id,s.name]),v=>d.suite_id=Number(v))),
    h('div',{class:'field'},h('label',{},'执行环境'),...state.environments.map(e=>checkbox(e.name,chosen.has(e.id),v=>v?chosen.add(e.id):chosen.delete(e.id)))),
    field('执行频率',frequencyBox),field('执行时间',input(clock,v=>{clock=v;updateCron();},'time')),field('Cron',cronBox,'五段表达式；直接编辑后使用自定义Cron。'),field('时区',input(d.timezone,v=>d.timezone=v)),
    button('预览执行时间',async()=>{const r=await request(projectAPI('schedules/preview'),'POST',{cron:d.cron,timezone:d.timezone});preview.replaceChildren(...r.times.map(t=>h('div',{},new Date(t*1000).toLocaleString('zh-CN',{timeZone:d.timezone,hour12:false})+' · '+d.timezone)));},'secondary'),preview,
    inheritControl,choices,state.notification_channels.length?null:h('p',{class:'notice'},'通知渠道未配置。可先保存计划，再到通知推送页面配置。'),field('结果推送范围',select(d.notify_mode||'inherit',[['inherit','使用项目默认'],['all','全部结果'],['failures','仅失败和异常']],v=>d.notify_mode=v==='inherit'?null:v)),checkbox('启用任务',d.enabled,v=>d.enabled=v)),
    ()=>{d.environment_ids=[...chosen];d.notification_channel_ids=inherit?null:[...selected];return saveSchedule(d);});
}
async function openBatch(id){const b=await request('/api/schedule-batches/'+id);modal('多环境执行批次',h('div',{class:'form-stack'},h('h3',{},b.name),h('p',{},date(b.scheduled_for)+' · '+(b.reason||statusLabel(b.status))),runTable(b.runs)),null,true);}
function statusLabel(status){return ({passed:'已通过',failed:'未通过',error:'执行异常',running:'执行中',queued:'排队中',cancelled:'已取消',skipped:'已跳过'})[status]||status;}
const channelNames={dingtalk_app:'钉钉企业内部应用',dingtalk_webhook:'钉钉存量Webhook',serverchan:'Server酱 · 个人微信'};
const deliveryNames={pending:'等待发送',sending:'提交中',accepted:'渠道已受理',failed:'失败',unknown:'状态未知',cancelled:'已取消'};
let deliveryPage=1;
async function notificationsPage(){
  const [channels,rule,result,health]=await Promise.all([request(projectAPI('notification-channels')),request(projectAPI('notification-rules')),request(projectAPI('notification-deliveries')+'?page='+deliveryPage),request('/api/health')]);state.health=health;
  state.notification_channels=channels;const admin=state.project.role==='admin';
  let selected=new Set(rule.channel_ids),triggers=new Set(rule.triggers),mode=rule.mode;
  const next=button('下一页',()=>{deliveryPage++;return renderPage();},'secondary small');next.disabled=deliveryPage*20>=result.total;
  const prev=button('上一页',()=>{deliveryPage--;return renderPage();},'secondary small');prev.disabled=deliveryPage<=1;
  return [heading('通知推送','测试完成后发送概要，完整报告仍需登录。受理成功不代表手机已经收到。',admin?button('新增通知渠道',()=>channelModal(),'','plus'):null),
    h('div',{class:'notice'+(state.health.notifier_online?'':' warning')},state.health.notifier_online?'通知执行器在线；今日渠道受理 '+result.today_submitted+' 条。':'通知执行器离线，消息将保留在队列。'),
    panel('接收渠道',channels.length?h('div',{class:'panel-body form-stack'},...channels.map(c=>h('div',{class:'resource-card'},h('h3',{},c.name),h('p',{},channelNames[c.kind]+' · '+(c.enabled?'已启用':'已关闭')),h('small',{},'配置责任人 #'+c.owner_id+'；密钥加密保存、不回显。'),admin?h('div',{class:'actions'},button('编辑渠道',()=>channelModal(c),'secondary small'),button('发送测试通知',async()=>{await request(projectAPI('notification-channels')+'/'+c.id+'/test','POST',{});toast('测试通知已入队，请查看发送记录并核对手机。');await renderPage();},'secondary small'),button('删除渠道',async()=>{if(!confirm('删除渠道并取消待发消息？'))return;await request(projectAPI('notification-channels')+'/'+c.id,'DELETE');await refresh();},'ghost small')):null))):empty('尚未配置渠道','管理员可添加钉钉或Server酱微信推送。')),
    admin?panel('项目默认推送规则',h('div',{class:'panel-body form-stack'},...channels.map(c=>checkbox(c.name,selected.has(c.id),v=>v?selected.add(c.id):selected.delete(c.id))),h('div',{class:'row wrap'},...Object.entries({schedule:'定时测试',manual:'手动测试',ci:'CI测试'}).map(([id,label])=>checkbox(label,triggers.has(id),v=>v?triggers.add(id):triggers.delete(id)))),field('默认结果范围',select(mode,[['all','全部结果'],['failures','仅失败和异常']],v=>mode=v)),button('保存推送规则',async()=>{await request(projectAPI('notification-rules'),'PUT',{channel_ids:[...selected],triggers:[...triggers],mode});toast('推送规则已保存，仅影响后续提交的任务。');},'secondary'))):null,
    panel('发送记录',h('div',{class:'panel-body form-stack'},h('p',{},'多环境批次只发送一条；状态未知时请先核对接收端，再决定重发。'),button('刷新发送记录',renderPage,'secondary small'),result.items.length?table(['渠道','状态','次数','时间 / 结果','操作'],result.items.map(d=>h('tr',{},cell(d.channel_name),cell(deliveryNames[d.status]),cell(d.attempts),cell(h('div',{},date(d.updated_at),h('div',{class:'subtext'},d.error||d.provider_id||'—'))),cell(h('div',{class:'actions'},d.event_key.startsWith('batch:')?button('查看批次',()=>openBatch(d.event_key.slice(6)),'ghost small'):d.event_key.startsWith('run:')?button('查看报告',()=>openReport(d.event_key.slice(4)),'ghost small'):null,admin&&['failed','unknown','cancelled'].includes(d.status)?button('重新发送',async()=>{if(!confirm('重发可能造成重复消息。已核对接收端并继续？'))return;await request(projectAPI('notification-deliveries')+'/'+d.id+'/retry','POST',{});await renderPage();},'secondary small'):null))))):empty('暂无发送记录','启用渠道并配置规则后，在下一次测试结束时生成通知。'),h('div',{class:'row wrap'},prev,h('span',{},deliveryPage+' / '+Math.max(1,Math.ceil(result.total/20))),next))),
    h('div',{class:'notice'},'Server酱Turbo目前免费每天5条，微信接收通道需在Server酱后台配置。报告默认是局域网链接，手机需接入可访问该虚拟机的网络。')];
}
function channelModal(existing=null){
  const d=existing?{...existing,credentials:{}}:{name:'',kind:'dingtalk_app',credentials:{},enabled:false};
  const fields=h('div',{class:'form-stack'});
  const draw=()=>{d.credentials={};const info={dingtalk_app:[['app_key','AppKey'],['app_secret','AppSecret'],['robot_code','robotCode'],['conversation_id','接收群ID'],['cool_app_code','酷应用编码（可选）']],dingtalk_webhook:[['webhook_url','Webhook地址'],['sign_secret','加签密钥（可选）'],['keyword','机器人关键词（可选）']],serverchan:[['sendkey','SendKey']]}[d.kind];fields.replaceChildren(...info.map(([key,label])=>field(label,input('',v=>d.credentials[key]=v,'password',{autocomplete:'new-password'}),existing?.configured_fields.includes(key)?'已配置；留空保留原值。':'')),h('p',{class:'notice'},d.kind==='serverchan'?'个人微信请选择Server酱Turbo并配置SCT类型SendKey；不支持SC3 App密钥。':d.kind==='dingtalk_webhook'?'仅兼容已有自定义机器人。需填写官方HTTPS地址，按机器人安全设置配置加签、关键词及IP白名单。':'需在钉钉创建并发布内部应用机器人，开通发消息权限并添加到接收群。'));};draw();
  modal(existing?'编辑通知渠道':'新增通知渠道',h('div',{class:'form-stack'},field('渠道名称',input(d.name,v=>d.name=v)),existing?h('p',{},channelNames[d.kind]):field('接入方式',select(d.kind,Object.entries(channelNames),v=>{d.kind=v;draw();})),fields,checkbox('启用结果推送',!!d.enabled,v=>d.enabled=v),h('p',{},'启用后允许向指定群或个人微信发送本项目结果摘要。完整请求、凭据和附件不会推送。')),
    ()=>request(projectAPI('notification-channels')+(existing?'/'+existing.id:''),existing?'PUT':'POST',{name:d.name,kind:d.kind,credentials:d.credentials,enabled:!!d.enabled}));
}
async function openLinkedReport(){
  const match=location.hash.match(/^#(run|batch)=([a-f0-9]{32})$/);if(!match||!state.user)return;
  const record=await request(match[1]==='run'?'/api/runs/'+match[2]:'/api/schedule-batches/'+match[2]);
  state.project=state.projects.find(p=>p.id===record.project_id);if(!state.project)throw Error('没有报告所属项目的权限');
  state.page=match[1]==='run'?'runs':'schedules';await refresh();if(match[1]==='run')await openReport(match[2]);else await openBatch(match[2]);window.history.replaceState(null,'',location.pathname);
}

function projectsPage(){
  return [heading('项目管理','以项目组织测试资产，按成员角色协作。',state.user.is_admin?button('创建项目',()=>projectModal(),'','plus'):null),
    h('div',{class:'cards-grid'},state.projects.map(project=>h('section',{class:'resource-card'},h('div',{class:'row'},h('div',{class:'stat-icon'},icon('folder')),h('span',{class:'flex-spacer'}),h('span',{class:'badge'},({admin:'管理员',tester:'测试人员',viewer:'只读成员'})[project.role])),
      h('h3',{},project.name),h('p',{},project.description||'集中管理项目用例、环境和执行结果。'),h('div',{class:'card-foot'},h('span',{},'项目 #'+project.id),h('div',{class:'actions'},project.role==='admin'?button('编辑',()=>projectModal(project),'ghost small'):null,button('进入项目',()=>act(async()=>{state.project=project;state.page='overview';localStorage.setItem('testdock-project',project.id);await refresh();}),'ghost small','arrow'))))))];
}
function projectModal(existing=null){
  const data=existing?{name:existing.name,description:existing.description}:{name:'',description:''};
  modal(existing?'编辑项目':'创建项目',h('div',{class:'form-stack'},field('项目名称',input(data.name,v=>data.name=v)),field('项目说明',area(data.description,v=>data.description=v))),
    async()=>{const created=await request('/api/projects'+(existing?'/'+existing.id:''),existing?'PUT':'POST',data);state.projects=await request('/api/projects');state.project=state.projects.find(p=>p.id===(existing?.id||created.id));});
}
async function teamPage(){
  const members=await request(projectAPI('members'));
  const content=[heading('成员与集成','管理协作权限，把测试接入团队交付流程。'),
    panel('项目成员',table(['成员','账号','角色','操作'],members.map(member=>h('tr',{},cell(h('strong',{},member.display_name)),cell(member.username),cell(({admin:'项目管理员',tester:'测试人员',viewer:'只读成员'})[member.role]),cell(admin()?h('div',{class:'actions'},button('修改角色',()=>memberModal(member),'ghost small'),iconButton('trash','移除成员',()=>act(async()=>{if(confirm('从项目移除 '+member.display_name+'？')){await request(projectAPI('members')+'/'+member.id,'DELETE');await renderPage();}}))):'—')))),admin()?button('添加成员',()=>memberModal(),'secondary small','plus'):null)];
  if(admin()){
    const tokens=await request(projectAPI('tokens'));
    content.push(panel('CI 访问令牌',tokens.length?table(['名称','创建时间','到期时间','操作'],tokens.map(token=>h('tr',{},cell(token.name),cell(date(token.created_at)),cell(date(token.expires_at)),cell(button('撤销令牌',()=>remove('tokens',token.id,token.name),'ghost small'))))):empty('为 CI 创建项目令牌','令牌只显示一次，可撤销，访问受项目权限限制。'),button('创建令牌',tokenModal,'secondary small','key')),
      h('div',{class:'notice'},'CI 入口：POST /api/ci/projects/'+state.project.id+'/runs。使用 Bearer 令牌和 Idempotency-Key，查询执行终态并下载 JUnit XML。详见项目运行说明。'));
    const audit=await request(projectAPI('audit'));
    content.push(panel('最近操作',audit.length?table(['时间','操作人','操作','对象'],audit.slice(0,12).map(row=>h('tr',{},cell(date(row.created_at)),cell(row.username||'系统'),cell(row.action),cell(row.detail)))):empty('暂无操作记录','新增和修改测试资产后将在此记录。')));
  }
  if(state.user.is_admin){
    const users=await request('/api/users');
    content.push(panel('系统账号',table(['姓名','账号','权限','状态','操作'],users.map(u=>h('tr',{},cell(h('strong',{},u.display_name)),cell(u.username),cell(u.is_admin?'系统管理员':'普通成员'),cell(h('span',{class:'badge '+(u.active?'passed':'cancelled')},u.active?'有效':'已停用')),cell(h('div',{class:'actions'},button('重设密码',()=>resetPasswordModal(u),'ghost small'),button(u.active?'停用':'启用',()=>act(async()=>{await request('/api/users/'+u.id,'PATCH',{active:!u.active});await renderPage();}),'ghost small')))))),button('创建账号',userModal,'secondary small','plus')));
  }
  return content;
}
async function memberModal(existing=null){
  await act(async()=>{
    const users=await request(projectAPI('available-users'));let id=existing?.id||users[0]?.id,role=existing?.role||'tester';
    modal(existing?'修改成员角色':'添加项目成员',h('div',{class:'form-stack'},field('用户',select(id,users.map(u=>[u.id,u.display_name+' · '+u.username]),v=>id=Number(v))),field('角色',select(role,[['admin','管理员：成员、令牌和测试资产'],['tester','测试人员：配置、编辑和执行'],['viewer','只读成员：查看资产和报告']],v=>role=v))),()=>request(projectAPI('members'),'PUT',{user_id:id,role}));
  });
}
function userModal(){
  const d={username:'',display_name:'',password:'',is_admin:false};
  modal('创建系统账号',h('div',{class:'form-stack'},field('账号',input('',v=>d.username=v),'英文开头，3至80位，支持数字、点、短横线'),field('姓名',input('',v=>d.display_name=v)),field('初始密码',input('',v=>d.password=v,'password',{autocomplete:'new-password'}),'至少10位；请通过安全方式交付给成员'),checkbox('系统管理员',false,v=>d.is_admin=v)),()=>request('/api/users','POST',d));
}
function resetPasswordModal(user){
  let password='';modal('重设 '+user.display_name+' 的密码',field('新密码',input('',v=>password=v,'password',{autocomplete:'new-password'}),'至少10位，保存后该用户的旧登录会话失效。'),()=>request('/api/users/'+user.id,'PATCH',{password}));
}
function tokenModal(){
  let name='',days=90;
  const body=h('div',{class:'form-stack'},field('令牌名称',input('',v=>name=v,'text',{placeholder:'例如 Jenkins 回归流水线'})),field('有效天数',input(days,v=>days=Number(v),'number',{min:1,max:365})));
  const dialog=modal('创建 CI 令牌',body);
  $('.modal-foot',dialog).append(button('创建并显示',()=>act(async()=>{
    const data=await request(projectAPI('tokens'),'POST',{name,expires_days:days});
    modal('请保存此令牌',h('div',{class:'form-stack'},h('div',{class:'notice warning'},'此令牌只显示一次。请保存到 CI 的凭据管理中。'),field('CI Bearer Token',h('textarea',{readOnly:true,class:'mono',onclick:e=>e.target.select()},data.token)),h('p',{class:'footnote'},'项目 ID：'+state.project.id+'。关闭后无法再次查看原令牌。')));
    await refresh();
  }),'','key'));
}
function accountModal(){
  let oldPassword='',newPassword='';
  const dialog=modal('账户设置',h('div',{class:'form-stack'},h('div',{class:'notice'},state.user.display_name+' · '+state.user.username),field('原密码',input('',v=>oldPassword=v,'password',{autocomplete:'current-password'})),field('新密码',input('',v=>newPassword=v,'password',{autocomplete:'new-password'}),'至少10位，修改后需要重新登录。')));
  $('.modal-foot',dialog).prepend(button('撤销 AI 用户委托',()=>act(async()=>{if(confirm('撤销后AI工作台将无法读取你的TestDock资料，需重新绑定。')){await request('/api/ai/revoke','POST',{});toast('AI用户委托已撤销');}}),'ghost'));
  $('.modal-foot',dialog).prepend(button('连接 AI 工作台',()=>act(async()=>{const result=await request('/api/ai/binding-code','POST',{});modal('一次性 AI 绑定码',h('div',{class:'form-stack'},h('p',{},'此码60秒有效，仅使用一次。登录AI工作台后，在模型与设置中绑定当前账号。'),field('绑定码',h('textarea',{readOnly:true,class:'mono'},result.code)),h('a',{href:result.workbench_url,target:'_blank',rel:'noopener'},'打开 AI 工作台')));}), 'secondary'));
  $('.modal-foot',dialog).prepend(button('退出登录',()=>act(async()=>{await request('/api/auth/logout','POST');state.user=null;renderLogin();}),'ghost','logout'));
  $('.modal-foot',dialog).append(button('修改密码',()=>act(async()=>{await request('/api/me/password','POST',{old_password:oldPassword,new_password:newPassword});state.user=null;renderLogin();toast('密码已更新，请重新登录');})));
}
async function remove(resource,id,name){
  if(!confirm('确认删除 / 撤销「'+name+'」？已有执行报告会保留。'))return;
  await act(async()=>{await request(projectAPI(resource)+'/'+id,'DELETE');await refresh();toast('已处理');});
}
setInterval(async()=>{
  if(!state.user)return;
  try{state.health=await request('/api/health');showHealth();if(state.project&&!$('#modal-root').children.length){if(state.page==='overview'){state.runs=await request(projectAPI('runs'));await renderPage();}else if(state.page==='runs'&&!document.activeElement?.closest('.history-filters'))await renderPage();}}catch{}
},5000);
try{state.user=await request('/api/me');await initialize();}catch{renderLogin();}

async function rerunReport(id,failedOnly,useLatest=false){
  await act(async()=>{const data=await request('/api/runs/'+id+'/rerun?failed_only='+failedOnly+'&use_latest='+useLatest,'POST',{});await openReport(data.id);});
}
async function exportAssets(){
  await act(async()=>{
    const bundle=await request(projectAPI('assets/export'));
    const url=URL.createObjectURL(new Blob([pretty(bundle)],{type:'application/json'}));
    const a=h('a',{href:url,download:'testdock-assets-'+state.project.id+'.json'});document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    toast('已导出。敏感变量值及命名凭据已移除，分享前请检查用例内容。');
  });
}
function importModal(){
  let text='',prefix='',bundle=null,warnings=[],required=[],selected=new Set();
  const output=h('div');const buttons=h('div',{class:'actions'});
  const source=area('',v=>{text=v;bundle=null;output.replaceChildren();buttons.replaceChildren();},{class:'mono',rows:8});
  const file=h('input',{type:'file',accept:'.json,application/json',onchange:async e=>{
    const f=e.target.files[0];if(!f)return;if(f.size>2*1024*1024){toast('文件超过2MB',true);return;}
    text=await f.text();source.value=text;bundle=null;output.replaceChildren();buttons.replaceChildren();
  }});
  function chosen(){const draft=clone(bundle);draft.cases=draft.cases.filter(c=>selected.has(c.key));draft.suites=draft.suites.map(s=>({...s,case_ids:s.case_ids.filter(k=>selected.has(k))})).filter(s=>s.case_ids.length);return draft;}
  function confirmButton(){buttons.replaceChildren(button('预检选中项',()=>act(async()=>{
    if(!selected.size&&!bundle.environments.length)throw new Error('至少选择一个用例或环境');
    const payload={bundle:chosen(),prefix,dry_run:true};const result=await request(projectAPI('assets/import'),'POST',payload);
    buttons.replaceChildren(h('span',{},'即将新增：'+result.counts.cases+' 用例 / '+result.counts.suites+' 套件 / '+result.counts.environments+' 环境'),button('确认导入',()=>act(async()=>{
      const done=await request(projectAPI('assets/import'),'POST',{...payload,dry_run:false});closeModal();await refresh();
      modal('导入完成',h('div',{class:'form-stack'},h('p',{},'资产已保存。请核对环境地址、变量、鉴权和断言，再执行测试。'),...done.warnings.map(w=>h('div',{class:'notice warning'},w))));
    })));
  }),'','check'));}
  const parse=()=>act(async()=>{
    const data=jsonParse(text,'导入文档');warnings=[];required=[];
    if(data.openapi){const preview=await request(projectAPI('openapi/preview'),'POST',{document:data});bundle=preview.bundle;warnings=preview.warnings;required=preview.required_variables;}
    else bundle=data;
    if(bundle.format!=='testdock-assets-v1'||!Array.isArray(bundle.cases))throw new Error('需要资产包或 OpenAPI 3 JSON');
    selected=new Set(bundle.cases.map(c=>c.key));
    output.replaceChildren(h('h3',{},'选择待导入用例 · '+bundle.cases.length),
      required.length?h('div',{class:'notice'},'待配置变量：'+required.join(', ')):null,
      ...warnings.map(w=>h('p',{class:'notice warning'},w)),
      h('div',{class:'checklist import-list'},bundle.cases.map(c=>checkbox(c.name,true,on=>{on?selected.add(c.key):selected.delete(c.key);confirmButton();}))),
      h('p',{class:'footnote'},'随包保留所选用例的套件与全部环境模板。导入只新增资产，不会执行请求。'));confirmButton();
  });
  modal('导入用例与环境',h('div',{class:'form-stack'},h('div',{class:'notice'},'支持 TestDock 资产包、OpenAPI 3.0/3.1 JSON。本地文件或粘贴文档均可；先预览再确认导入。'),
    field('读取 JSON 文件',file),field('JSON 文档',source),field('名称前缀',input('',v=>{prefix=v;if(bundle)confirmButton();}),'同名资产不会覆盖，可填写前缀。'),button('解析文档',parse,'secondary'),output,buttons),null,true);
}

function guidePage(){
  const steps=[
    ['1 · 建立项目边界','按业务系统创建项目，添加成员并设置管理员、测试人员或只读角色。平台的 Web/API 步骤与销售、供应链、后台管理等业务无关，可复用同一套操作。'],
    ['2 · 添加被测环境','为开发、测试和预发分别配置 Web/API 基础地址。普通数据放普通变量；密码、Token 和密钥放敏感变量。用 {{变量名}} 引用。'],
    ['3 · 接入已有接口或编排步骤','在测试用例中选择导入，上传或粘贴 OpenAPI 3.0/3.1 JSON；先解析、勾选接口、预检，再确认。根据提示配置参数和鉴权。没有文档时用表单编排 Web 与 HTTP 步骤。'],
    ['4 · 组织可重复的业务链路','把登录、查询、创建与核对放进同一个用例，用响应提取传递 Token、商品ID、订单ID。每个用例独立会话和变量。断言业务值、状态码和关键页面内容。'],
    ['5 · 在多个环境执行','将用例加入有序套件，启动时选择主环境和其他环境。每个环境生成独立报告；页面可按环境、状态、触发方式和时间查询全部历史。时间筛选使用浏览器本地时区。'],
    ['6 · 定位并验证修复','从失败报告查看请求/响应、断言和截图。复现快照可保留当时配置；修复用例或环境后，选择按最新配置重跑未通过。历史报告始终保留。'],
    ['7 · 接入持续回归','为稳定套件配置 Cron 与时区，或创建 CI 令牌让流水线提交、查询终态并收集 JUnit。先在测试环境确认写入类操作的测试数据及清理方式。']
  ];
  return [heading('项目接入指南','从已有接口和页面操作，建立可持续运行的业务回归。'),
    panel('通用接入流程',h('div',{class:'panel-body form-stack'},steps.map(([title,detail])=>h('div',{},h('h3',{},title),h('p',{},detail))))),
    panel('业务示例 · 化工品销售',h('div',{class:'panel-body'},h('p',{},'商品查询 → 获取 SKU → 报价 → 提交订单 → 核对金额与订单状态。示例同时包含页面与接口用例，可切换两套演示环境。'),
      h('a',{class:'button secondary',href:projectAPI('examples/sales'),download:'chemical-sales.json'},'下载销售流程示例'),
      h('p',{class:'footnote'},'建议在独立示例项目导入，环境敏感变量 password 填写公开演示值 demo-pass。演示服务只创建内存中的虚构报价和订单，重启后清空；测试报告保留。用于自己的业务时替换地址、字段、鉴权与断言。'))),
    panel('导入和分享的约定',h('div',{class:'panel-body'},h('p',{},'资产包可迁移用例、套件与环境模板，敏感变量需在目标环境重新配置。命名凭据字段会清空，分享前仍需检查正文和普通变量。单包不超过 2MB、1000 用例；同名资产不会覆盖，可设置名称前缀。'),h('p',{},'OpenAPI 导入支持本地引用和常见参数、JSON/form 请求体、Bearer/API Key 模板；复杂组合 schema、文件上传、外部引用等需要手动编排。生成用例须核对数据、鉴权和预期状态码后执行。')))];
}

window.addEventListener('hashchange',()=>act(openLinkedReport));

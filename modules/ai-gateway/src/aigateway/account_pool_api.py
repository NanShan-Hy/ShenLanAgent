"""A bounded, operator-only facade for the dedicated loopback account backend."""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import Request
from pydantic import Field
from fastapi.responses import HTMLResponse, RedirectResponse

from .account_pool_config import load_connector
from .core import audit, digest, uid
from .http_pool import client_for
from .models import Model, ModelInput
from .security import GatewayError, admin, fail, tenant_access

PREFIX='/api/v1/admin/account-pool'


def oauth_failure(raw):
    """Classify bounded backend text, never return or persist that text."""
    raw = raw[:16384].lower() if isinstance(raw,str) else ''
    code, message, http_status = 'authorization_failed', '账号登录未完成，请重新登录；如仍失败，请检查账号池服务。', None
    if 'unsupported_country_region_territory' in raw:
        code,message,http_status='region_unsupported','认证服务不支持服务器当前出口所在国家或地区（HTTP 403），请核对服务器实际出口与服务可用地区；重复登录或提交结果页不能解决。',403
    elif 'unknown or expired' in raw or 'timeout waiting for oauth callback' in raw:
        code,message='authorization_expired','登录已过期，请重新生成链接并在五分钟内完成。'
    elif 'state' in raw and ('invalid' in raw or 'error' in raw or 'match' in raw):
        code,message='callback_state_mismatch','回调与本次登录不匹配，请复制本次登录返回的完整地址。'
    elif 'invalid_grant' in raw or 'code has expired' in raw or 'already redeemed' in raw:
        code,message='authorization_code_rejected','授权码已失效或已使用，请重新登录，不要重复提交旧地址。'
    elif 'save' in raw and ('token' in raw or 'credential' in raw) or 'persist oauth callback' in raw:
        code,message='credential_save_failed','账号凭据保存失败，请检查账号池数据目录权限和磁盘空间。'
    elif 'access_denied' in raw or raw.strip() in ('bad request','access denied'):
        code,message='authorization_denied','本次登录授权被拒绝或取消，请重新登录。'
    elif 'exchange' in raw and (match := re.search(r'(?:status|http)[ :]*(\d{3})',raw)):
        http_status=int(match[1]);code='token_exchange_rejected'
        message=f'认证服务拒绝令牌交换（HTTP {http_status}），请检查服务器出口网络和账号访问权限后重新登录。'
    elif any(v in raw for v in ('timeout','timed out','deadline exceeded','connection refused','no such host','tls','dial tcp','network is unreachable')):
        code,message='authorization_network_failed','服务器连接认证服务失败，请检查服务器的DNS、网络或出站代理。'
    elif 'exchange' in raw:
        code,message='token_exchange_failed','浏览器登录已返回，但服务器交换登录凭据失败，请检查认证服务连通性后重新登录。'
    return {'error_code':'account_pool_'+code,'message':message,'http_status':http_status}


def flow_session_active(api,flow):
    return api.state.db.one('''SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.hash=? AND s.user_id=? AND s.expires_at>? AND u.active=1 AND u.is_admin=1''',
        (flow['session'],flow['owner'],time.time())) is not None


def flow_event(api,pk,flow,phase,**values):
    changed=flow.get('phase')!=phase or any(flow.get(k)!=v for k,v in values.items())
    flow.update(phase=phase,**values)
    if changed:
        with api.state.db.transaction() as c:
            audit(c,flow['owner'],None,'account_pool.authorization_'+phase,pk,
                json.dumps({k:flow.get(k) for k in ('phase','error_code','http_status')},ensure_ascii=False))


async def submit_callback(api,pk,flow,params):
    # No await before claiming this in-memory flow: auto/manual concurrent submissions
    # share this gate in the single event loop and cannot exchange the code twice.
    if not flow_session_active(api,flow):
        fail(401,'account_pool_session_ended','发起登录的会话已结束，请回到控制台重新登录。')
    if flow['expires_at']<=time.time():
        fail(410,'account_pool_flow_expired','账号登录已过期，请重新发起。')
    if flow['status']!='pending':
        fail(409,'account_pool_callback_consumed','回调已提交，请等待状态确认；不会重复交换授权码。')
    if (len(params.get('state',[]))!=1
            or not hmac.compare_digest(params['state'][0].encode(),flow['state'].encode())
            or not ((len(params.get('code',[]))==1 and 1<=len(params['code'][0])<=8192 and not params.get('error'))
                or (len(params.get('error',[]))==1 and not params.get('code')))):
        fail(400,'account_pool_callback_invalid','请提交本次登录返回的完整地址，state和授权码必须有效。')
    flow['status']='submitted'
    flow_event(api,pk,flow,'exchanging')
    payload={'provider':'codex','state':flow['state']}
    payload.update({'code':params['code'][0]} if params.get('code') else {'error':'access_denied'})
    try:
        await backend(api,'POST','/oauth/callback',body=payload)
    except BaseException:
        flow['status']='uncertain'
        flow_event(api,pk,flow,'uncertain',message='回调接收结果待确认，正在查询状态，请勿重复提交。')
        raise
    return {'status':'submitted','phase':'exchanging','token_exchange_complete':False}


class BootstrapInput(Model):
    tenant_id: str
    platform_shared: bool = False
    sharing_consent: bool = False


class AuthorizeInput(Model):
    sharing_consent: bool = False


class CallbackInput(Model):
    redirect_url: str = Field(min_length=1,max_length=12000)


class BatchInput(Model):
    ids: list[str] = Field(min_length=1,max_length=20)
    action: str = Field(pattern=r'^(enable|disable|refresh)$')


class PublishInput(Model):
    model_ids: list[str] = Field(min_length=1,max_length=50)
    alias_prefix: str = Field(default='gpt/',max_length=30,pattern=r'^[a-zA-Z0-9_.:/-]*$')
    input_rate: int = Field(ge=0,le=10**12)
    cache_rate: int = Field(ge=0,le=10**12)
    output_rate: int = Field(ge=0,le=10**12)
    context_limit: int = Field(default=131072,ge=2048,le=2000000)
    max_output: int = Field(default=8192,ge=1,le=100000)


def connector(db):
    try:
        value=load_connector(db.settings)
    except (OSError,ValueError,TypeError):
        fail(503,'account_pool_configuration','本机账号池连接配置不可用，请检查独立服务配置和权限')
    if not value:
        fail(503,'account_pool_not_configured','本机账号池尚未连接，请先完成服务部署配置')
    return value


async def backend(api,method,path,*,body=None,params=None,management=True):
    """Fixed caller-chosen paths only; no user-supplied URL, redirects, or backend text errors."""
    if (api.state.db.settings.data_dir/'maintenance').exists():
        fail(503,'maintenance','网关维护中，请稍后重试账号池操作')
    config=connector(api.state.db)
    root=config['base_url'].removesuffix('/v1')
    url=root+('/v8/management' if management else '/v1')+path
    key=config['management_key'] if management else config['client_key']
    client=client_for(api)
    try:
        async with asyncio.timeout(12):
            async with client.stream(method,url,headers={'Authorization':'Bearer '+key},json=body,params=params,timeout=10) as response:
                content=bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>1024**2:fail(502,'account_pool_response_size','账号池响应超过上限')
                if not 200<=response.status_code<300:
                    if path=='/oauth/callback':
                        try: problem=oauth_failure(json.loads(content).get('error'))
                        except (ValueError,AttributeError): problem=oauth_failure('')
                        fail(502,problem['error_code'],problem['message'])
                    fail(502,'account_pool_backend_'+str(response.status_code),'账号池操作未通过，请检查服务状态或重新授权')
                value=json.loads(content)
                if not isinstance(value,dict):raise ValueError()
                return value
    except (httpx.HTTPError,TimeoutError):
        fail(503,'account_pool_unreachable','无法连接本机账号池，请检查gpt-account-pool服务')
    except (ValueError,UnicodeError):
        fail(502,'account_pool_invalid_response','账号池返回格式无效')


async def accounts(api):
    data=await backend(api,'GET','/credentials')
    files=data.get('files')
    if not isinstance(files,list) or len(files)>500:
        fail(502,'account_pool_invalid_response','账号池列表无效或超过500项')
    result=[]
    for value in files:
        if not isinstance(value,dict) or value.get('provider',value.get('type'))!='codex':continue
        name=value.get('name')
        if not isinstance(name,str) or not 1<=len(name)<=255 or any(c in name for c in '/\\\r\n\x00'):continue
        status=value.get('status')
        if status not in ('active','disabled','error','unavailable','pending'):status='unknown'
        label=value.get('label') or value.get('email') or name
        result.append({'id':digest(name)[:32],'label':str(label)[:120],
            'account_reference':digest(str(value.get('id') or name))[:12],
            'status':status,'disabled':bool(value.get('disabled')),'unavailable':bool(value.get('unavailable')),
            'inference_verified':value.get('inference_verified') is True,
            'renewal_verified':value.get('renewal_verified') is True,
            'requires_sign_in':value.get('requires_sign_in') is True,
            'next_retry_at':value.get('next_retry_after') if isinstance(value.get('next_retry_after'),str) else None,
            'usage_remaining':None,'_name':name})
    return result


def public_account(row):
    return {k:v for k,v in row.items() if k!='_name'}


def live_flow(api,request,pk):
    user=admin(api.state.db,request)
    flows=getattr(api.state,'account_pool_flows',{})
    flow=flows.get(pk)
    if not flow or flow['owner']!=user['id'] or flow['session']!=digest(request.cookies.get('gateway_session','')):
        fail(404,'account_pool_flow_not_found','授权事务不存在或不属于当前登录会话')
    if flow['expires_at']<=time.time():
        flows.pop(pk,None)
        fail(410,'account_pool_flow_expired','账号授权已过期，请重新发起')
    if not flow_session_active(api,flow):
        fail(401,'account_pool_session_ended','发起登录的会话已结束，请重新登录。')
    return flow


def attach_account_pool(api,db):
    @api.get(PREFIX+'/status')
    async def status(request: Request):
        admin(db,request)
        result={'configured':db.settings.account_pool_config is not None,'online':False,'accounts':[],
            'pool':None,'upstream':None,'models':[],'catalog':[],'error_code':'','generation_tested':False}
        try:
            result['accounts']=[public_account(r) for r in await accounts(api)]
            await backend(api,'GET','/models',management=False)
            admin(db,request)
            result['online']=True
        except GatewayError as error:
            result['error_code']=error.code
        with db.connect() as c:
            upstream=c.execute("SELECT * FROM upstreams WHERE provider='account_pool' ORDER BY created_at LIMIT 1").fetchone()
            if upstream:
                pool=dict(c.execute('SELECT * FROM pools WHERE id=?',(upstream['pool_id'],)).fetchone())
                result['pool']=pool
                result['upstream']={k:upstream[k] for k in ('id','enabled','revision')}
                result['models']=[dict(r) for r in c.execute('SELECT id,alias,enabled,input_rate,cache_rate,output_rate FROM models WHERE pool_id=?',(pool['id'],))]
                result['catalog']=[r[0] for r in c.execute('SELECT model_id FROM upstream_catalog WHERE upstream_id=? AND revision=? ORDER BY model_id',(upstream['id'],upstream['revision']))]
        steps=[('backend','账号池服务',result['online']),('pool','网关资源池',bool(result['pool'] and result['pool']['enabled'])),
            ('accounts','添加并启用GPT账号',any(not a['disabled'] and not a['unavailable'] and not a['requires_sign_in'] and a['status']=='active' for a in result['accounts'])),
            ('catalog','同步模型目录',bool(result['catalog'])),
            ('models','发布模型与资费',any(m['enabled'] for m in result['models']))]
        result['steps']=[{'id':key,'label':label,'done':bool(done)} for key,label,done in steps]
        result['ready']=all(done for _,_,done in steps)
        result['generation_tested']=any(a['inference_verified'] for a in result['accounts'])
        return result

    @api.post(PREFIX+'/bootstrap',status_code=201)
    def bootstrap(body: BootstrapInput,request: Request):
        user=admin(db,request);config=connector(db)
        if not body.sharing_consent:fail(400,'account_pool_consent_required','请确认本机账号池可在所选范围内承接项目任务')
        with db.transaction() as c:
            tenant_access(c,user['id'],body.tenant_id,'admin')
            old=c.execute("SELECT id,pool_id FROM upstreams WHERE provider='account_pool'").fetchone()
            if old:
                return {'pool_id':old['pool_id'],'upstream_id':old['id'],'created':False}
            pool_id,upstream_id,now=uid(),uid(),time.time()
            c.execute('''INSERT INTO pools(id,name,kind,visibility,tenant_id,quota_group,enabled,rpm,tpm,concurrency,created_at)
                VALUES(?,?,'api',?,?,?,1,60,300000,10,?)''',
                (pool_id,'GPT账号池','platform' if body.platform_shared else 'tenant',None if body.platform_shared else body.tenant_id,'local-gpt-pool',now))
            c.execute('''INSERT INTO upstreams(id,pool_id,name,provider,base_url,secret,enabled,concurrency,created_at)
                VALUES(?,?,?,'account_pool',?,?,1,10,?)''',
                (upstream_id,pool_id,'本机GPT账号池',config['base_url'],db.settings.seal({'api_key':config['client_key']}),now))
            audit(c,user['id'],body.tenant_id,'account_pool.bootstrap',pool_id,'platform' if body.platform_shared else 'tenant')
        return {'pool_id':pool_id,'upstream_id':upstream_id,'created':True}

    @api.post(PREFIX+'/authorize')
    async def authorize(body: AuthorizeInput,request: Request):
        user=admin(db,request)
        if not body.sharing_consent:fail(400,'account_pool_consent_required','请确认新增账号可供当前GPT资源池承接任务')
        flows=getattr(api.state,'account_pool_flows',{})
        api.state.account_pool_flows=flows
        now=time.time()
        for pk in [pk for pk,f in flows.items() if f['expires_at']<=now]:flows.pop(pk,None)
        pending=[f for f in flows.values() if f['status'] not in ('completed','failed','cancelled')]
        if len(pending)>=32 or sum(f['owner']==user['id'] for f in pending)>=3:
            fail(429,'account_pool_authorization_capacity','未完成授权过多，请完成或取消已有授权')
        with db.transaction() as c:
            if c.execute("SELECT count(*) FROM audit WHERE user_id=? AND action='account_pool.authorize' AND created_at>?",(user['id'],now-600)).fetchone()[0]>=5:
                fail(429,'account_pool_authorization_rate','十分钟内最多发起五次授权')
            audit(c,user['id'],None,'account_pool.authorize')
        data=await backend(api,'POST','/oauth/auth-url',body={'provider':'codex','is_webui':False})
        url=data.get('url','');state=data.get('state','')
        parsed=urlparse(url);query=parse_qs(parsed.query)
        redirect=query.get('redirect_uri',[''])[0]
        if (parsed.scheme!='https' or parsed.netloc!='auth.openai.com' or parsed.path!='/oauth/authorize'
                or parsed.fragment or parsed.username or parsed.password or not isinstance(state,str) or not 1<=len(state)<=256
                or query.get('state')!=[state] or query.get('code_challenge_method')!=['S256']
                or redirect not in ('http://localhost:1455/auth/callback','http://127.0.0.1:1455/auth/callback')):
            fail(502,'account_pool_authorization_response','账号池授权链接不符合预期，已拒绝打开')
        if any(f['state']==state for f in flows.values()):
            fail(502,'account_pool_authorization_response','账号池返回了重复登录事务，请稍后重试。')
        pk=uid();flows[pk]={'state':state,'owner':user['id'],'session':digest(request.cookies.get('gateway_session','')),
            'expires_at':now+300,'redirect':redirect,'status':'pending','phase':'awaiting_login',
            'saved':False,'adopted':False,'error_code':'','message':'等待浏览器登录并返回。'}
        return {'id':pk,'authorization_url':url,'expires_at':now+300,'callback_mode':'auto_with_paste_fallback',
            'ssh_forward':'-L localhost:1455:127.0.0.1:'+str(db.settings.port)}

    @api.post(PREFIX+'/models')
    def publish_models(body: PublishInput,request: Request):
        user=admin(db,request)
        if len(set(body.model_ids))!=len(body.model_ids) or any(not 1<=len(m)<=120 for m in body.model_ids):
            fail(400,'account_pool_invalid_models','模型列表包含重复项或无效模型ID')
        if body.input_rate+body.output_rate<=0 or body.max_output>=body.context_limit:
            fail(400,'account_pool_pricing','请填写有效平台资费，并让输出预算小于上下文限制')
        result=[];aliases=set()
        with db.transaction() as c:
            upstream=c.execute("SELECT * FROM upstreams WHERE provider='account_pool' ORDER BY created_at LIMIT 1").fetchone()
            if not upstream:fail(409,'account_pool_bootstrap_required','请先创建网关账号池连接')
            catalog={r[0] for r in c.execute('SELECT model_id FROM upstream_catalog WHERE upstream_id=? AND revision=?',(upstream['id'],upstream['revision']))}
            for model in body.model_ids:
                if model not in catalog:fail(409,'account_pool_catalog_changed','所选模型不在当前目录中，请重新同步并选择')
                alias=(body.alias_prefix+re.sub(r'[^a-zA-Z0-9_.:/-]','-',model))[:100]
                if alias in aliases:fail(409,'account_pool_alias_collision','模型别名发生重复，请调整前缀后分批发布')
                aliases.add(alias)
                values=ModelInput(alias=alias,pool_id=upstream['pool_id'],provider='account_pool',upstream_model=model,
                    protocols=['chat','responses'],enabled=True,input_rate=body.input_rate,cache_rate=body.cache_rate,
                    output_rate=body.output_rate,context_limit=body.context_limit,max_output=body.max_output).model_dump()
                values['protocols']=json.dumps(values['protocols'])
                old=c.execute('SELECT * FROM models WHERE alias=?',(alias,)).fetchone()
                if old and any(old[k]!=values[k] for k in ('pool_id','provider','upstream_model')):
                    fail(409,'account_pool_alias_collision','模型别名已用于其他资源，请调整前缀')
                version=old['price_version'] if old else 1
                if old and any(old[k]!=values[k] for k in ('input_rate','cache_rate','output_rate')):version+=1
                values['price_version']=version
                if old:
                    pk=old['id']
                    c.execute('UPDATE models SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),pk))
                else:
                    pk=uid();values={'id':pk,**values,'created_at':time.time()}
                    c.execute('INSERT INTO models('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
                audit(c,user['id'],None,'account_pool.publish_model',pk)
                result.append({'id':pk,'alias':alias})
        return {'models':result,'count':len(result)}

    @api.post(PREFIX+'/authorizations/{pk}/callback')
    async def callback(pk: str,body: CallbackInput,request: Request):
        flow=live_flow(api,request,pk)
        parsed=urlparse(body.redirect_url.strip());expected=urlparse(flow['redirect']);params=parse_qs(parsed.query)
        if parsed.scheme==expected.scheme and parsed.netloc==expected.netloc and parsed.path=='/auth/callback/result':
            fail(400,'account_pool_callback_result_page','这是自动回调后的结果页地址，无需再次提交。请查看控制台最终登录状态，失败时按具体原因处理。')
        if (parsed.scheme!=expected.scheme or parsed.netloc!=expected.netloc or parsed.path!=expected.path
                or parsed.fragment or parsed.username or parsed.password):
            fail(400,'account_pool_callback_invalid','请粘贴本次授权返回的完整localhost:1455地址，state必须匹配')
        return await submit_callback(api,pk,flow,params)

    @api.get('/auth/callback',include_in_schema=False)
    async def automatic_callback(request: Request):
        # The browser's localhost:1455 is SSH-forwarded to this gateway. Cookies
        # on 127.0.0.1:8094 need not be sent to localhost:1455: bind by the original
        # still-active portal session and the unpredictable, single-use OAuth state.
        try:
            if len(request.url.query)>12000:
                fail(400,'account_pool_callback_invalid','回调地址过长。')
            params=parse_qs(request.url.query)
            state=params.get('state',[])
            if len(state)!=1 or not 1<=len(state[0])<=256:
                fail(400,'account_pool_callback_invalid','登录回调无效。')
            match=next(((pk,f) for pk,f in getattr(api.state,'account_pool_flows',{}).items()
                if hmac.compare_digest(f['state'].encode(),state[0].encode())),None)
            if not match:fail(404,'account_pool_flow_not_found','登录事务不存在或已失效。')
            await submit_callback(api,*match,params)
            location='/auth/callback/result?status=accepted'
        except GatewayError:
            location='/auth/callback/result?status=check_console'
        return RedirectResponse(location,status_code=303,headers={'Referrer-Policy':'no-referrer'})

    @api.get('/auth/callback/result',include_in_schema=False)
    def callback_result(status: str=''):
        if status=='accepted':
            heading='登录回调已接收'
            detail='请返回原来的 AI Gateway 控制台标签页，等待最终登录结果。'
        else:
            heading='请返回 AI Gateway 控制台查看结果'
            detail='未确认本次回调已接收，请查看原控制台的具体提示，必要时重新发起登录。'
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>GPT账号登录</title>'
            '<h1>'+heading+'</h1><p>'+detail+'</p>'
            '<p>这是结果页，无需复制地址或再次手动提交。回调已接收不代表账号登录已经成功。</p>'
            '<p>如果控制台提示 HTTP 403 或地区限制，请先检查服务器出口与服务可用地区，重复提交本页地址无法解决。</p>'
            '</html>',headers={'Referrer-Policy':'no-referrer'})

    @api.get(PREFIX+'/authorizations/{pk}')
    async def flow_status(pk: str,request: Request):
        flow=live_flow(api,request,pk)
        data=await backend(api,'GET','/oauth/status',params={'state':flow['state']})
        state=data.get('status')
        if state not in ('wait','ok','error'):fail(502,'account_pool_invalid_response','账号池授权状态无效')
        if flow['status']=='completed':state='ok'
        elif state=='ok':
            flow['saved']=data.get('saved') is True;flow['adopted']=data.get('adopted') is True
            if not flow['saved']:
                state='error';flow['status']='failed'
                flow_event(api,pk,flow,'failed',**oauth_failure('Failed to save authentication tokens'))
            elif not flow['adopted']:
                state='wait'
                if data.get('adoption_error'):
                    flow_event(api,pk,flow,'loading',error_code='account_pool_credential_load_failed',
                        message='凭据已保存，但账号池运行时载入失败，请检查账号池服务；无需重复登录。')
                else:
                    flow_event(api,pk,flow,'loading',error_code='account_pool_credential_loading',message='凭据已保存，正在等待账号池载入；请勿重复登录。')
            else:
                auth_id=data.get('auth_id')
                current=await accounts(api)
                account=next((a for a in current if a['_name']==auth_id),None)
                if not account:
                    state='wait';flow_event(api,pk,flow,'loading',message='正在确认账号列表，请稍候。')
                else:
                    flow['status']='completed';flow['account_id']=account['id']
                    flow_event(api,pk,flow,'ready',error_code='',http_status=None,message='账号已登录并载入，可继续配置模型和项目Key。')
        elif state=='error':
            flow['status']='failed';flow_event(api,pk,flow,'failed',**oauth_failure(data.get('error')))
        return {**{k:flow.get(k) for k in ('phase','error_code','message','http_status','saved','adopted','account_id')},
            'status':state,'expires_at':flow['expires_at']}

    @api.delete(PREFIX+'/authorizations/{pk}')
    async def cancel(pk: str,request: Request):
        flow=live_flow(api,request,pk)
        await backend(api,'DELETE','/oauth/session',params={'state':flow['state']})
        api.state.account_pool_flows.pop(pk,None)
        return {'cancelled':True}

    @api.post(PREFIX+'/accounts/batch')
    async def batch(body: BatchInput,request: Request):
        user=admin(db,request);current={a['id']:a for a in await accounts(api)};results=[]
        if len(set(body.ids))!=len(body.ids):fail(400,'account_pool_duplicate_selection','请勿重复选择同一账号')
        for pk in body.ids:
            admin(db,request)
            account=current.get(pk)
            if not account:
                results.append({'id':pk,'ok':False,'error_code':'account_pool_account_not_found'});continue
            try:
                if body.action=='refresh':await backend(api,'POST','/credentials/refresh',body={'name':account['_name']})
                else:await backend(api,'PATCH','/credentials/status',body={'name':account['_name'],'disabled':body.action=='disable'})
                with db.transaction() as c:audit(c,user['id'],None,'account_pool.'+body.action,pk)
                results.append({'id':pk,'ok':True})
            except GatewayError as error:results.append({'id':pk,'ok':False,'error_code':error.code})
        return {'results':results}

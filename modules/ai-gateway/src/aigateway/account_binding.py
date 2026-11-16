"""Durable, credential-specific pins. Only gateway-to-loopback headers carry auth IDs."""
import json
import re
import time

import httpx
from cryptography.fernet import InvalidToken

from .account_pool_config import load_connector
from .core import digest
from .security import fail


def account_id(db,value):
    try:
        result=db.settings.unseal(value)['id']
        if not isinstance(result,str):raise ValueError('invalid account binding')
        return result
    except (InvalidToken,ValueError,KeyError,TypeError):fail(503,'account_binding_unreadable','账号绑定无法解密，请管理员检查或明确迁移，不会隐式换账号')


def portable(items):
    """Keep visible messages and call IDs; supplier-private state does not cross accounts."""
    result=[]
    for item in items:
        if not isinstance(item,dict):result.append(item);continue
        if item.get('type')=='reasoning':continue
        value={k:v for k,v in item.items() if k not in ('encrypted_content',)}
        if value.get('type') in ('message','function_call','custom_tool_call','tool_search_call'):value.pop('id',None)
        result.append(value)
    return result


def policy_for(db,application,task_type=''):
    rows=db.all("SELECT * FROM conversation_policies WHERE application_id IN ('',?) AND task_type IN ('',?)",(application,task_type))
    rows.sort(key=lambda r:(r['application_id']==application,r['task_type']==task_type and bool(task_type)),reverse=True)
    return rows[0] if rows else {'policy':'continuity','identity_mode':'key_owner'}


async def bind(request,admitted,session):
    db=request.app.state.db;upstream=admitted['upstream']
    if upstream['provider']!='account_pool':return None
    turn=getattr(request.state,'managed_turn',None)
    conversation=turn.conversation if turn else getattr(request.state,'binding_conversation',None)
    try:config=load_connector(db.settings)
    except (OSError,ValueError,TypeError):fail(503,'account_pinning_unavailable','账号池连接配置不可用，尚未发送模型请求')
    if not config:fail(503,'account_pinning_unavailable','账号池连接配置不可用')
    if not config.get('pinning_supported'):
        if conversation:fail(503,'account_pinning_unavailable','账号池尚未启用持久账号绑定')
        return None
    key=admitted['key'];scope=conversation['id'] if conversation else digest(key['user_id']+'\0'+key['application_id']+'\0'+admitted['model']['id']+'\0'+session)
    policy=conversation['policy'] if conversation else policy_for(db,key['application_id'])['policy']
    catalog=getattr(request.app.state,'binding_catalog',None)
    if catalog is not None:
        files=catalog()
    else:
        try:
            async with httpx.AsyncClient(trust_env=False,timeout=5) as client:
                response=await client.get(config['base_url'].removesuffix('/v1')+'/v8/management/gateway-accounts',params={'model':admitted['model']['upstream_model']},headers={'Authorization':'Bearer '+config['management_key']})
                response.raise_for_status();files=response.json()['accounts']
        except (httpx.HTTPError,ValueError,KeyError):fail(503,'account_catalog_unavailable','无法确认上游账号状态，尚未发送模型请求')
    available=[]
    for f in files:
        if f.get('provider',f.get('type'))!='codex' or any(f.get(k) for k in ('disabled','requires_sign_in','quota_blocked','unavailable')):continue
        pk=f.get('id')
        if isinstance(pk,str) and re.fullmatch(r'[!-~]{1,512}',pk):available.append(pk)
    old=db.one('SELECT * FROM account_bindings WHERE id=?',(scope,))
    previous=account_id(db,old['account_id']) if old else None
    excluded=json.loads(old['excluded']) if old else []
    blocked=set(excluded) if old and old['state']=='unavailable' and old['updated_at']>time.time()-60 else set()
    if previous in available and digest(previous) not in blocked:selected=previous
    elif previous and policy=='strict':fail(503,'session_account_unavailable','任务固定账号暂不可用，请等待恢复或明确迁移')
    else:
        eligible=[pk for pk in available if digest(pk) not in blocked and pk not in blocked]
        if not eligible:fail(503,'session_account_unavailable','没有可用账号，任务历史已保留')
        # Fair initial placement across durable bindings; no OAuth material leaves the backend.
        refs={db.settings.key_hash(pk):pk for pk in eligible};counts={pk:0 for pk in eligible}
        for row in db.all('SELECT account_ref,count(*) n FROM account_bindings WHERE account_ref IN ('+','.join('?' for _ in refs)+') GROUP BY account_ref',tuple(refs)):
            counts[refs[row['account_ref']]]=row['n']
        selected=min(eligible,key=lambda x:(counts[x],x))
    with db.transaction() as c:
        # A lease serializes managed turns. Native client scopes may race only on initial
        # binding: use the first committed valid pin rather than issuing two credentials.
        current=c.execute('SELECT * FROM account_bindings WHERE id=?',(scope,)).fetchone()
        if not old and current:selected=account_id(db,current['account_id'])
        encrypted=db.settings.seal({'id':selected})
        c.execute("INSERT INTO account_bindings(id,account_id,policy,excluded,state,updated_at,account_ref) VALUES(?,?,?,'[]','bound',?,?) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,policy=excluded.policy,state='bound',excluded='[]',updated_at=excluded.updated_at,account_ref=excluded.account_ref",(scope,encrypted,policy,time.time(),db.settings.key_hash(selected)))
        if previous!=selected:
            c.execute('INSERT INTO binding_events(binding_id,old_account,new_account,reason,created_at) VALUES(?,?,?,?,?)',
                (scope,old['account_id'] if old else None,encrypted,'initial' if not previous else 'account_unavailable',time.time()))
        c.execute('UPDATE conversation_turns SET account_ref=? WHERE request_id=?',(digest(selected),admitted['id']))
    if previous and previous!=selected and admitted['protocol']!='chat':
        from .media import copy_budget
        payload=dict(admitted['payload'])
        if isinstance(payload.get('input'),list):payload['input']=portable(payload['input'])
        admitted['payload']=copy_budget(admitted['payload'],payload)
    request.state.account_binding=(scope,selected)
    return selected


def failed(db,binding,status):
    if binding and status in (401,402,403,404,429):
        with db.transaction() as c:
            c.execute("UPDATE account_bindings SET state='unavailable',excluded=?,updated_at=? WHERE id=?",(json.dumps([digest(binding[1])]),time.time(),binding[0]))

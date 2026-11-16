import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from aigateway.billing import ledger_check, maintenance, mark_unknown, settle
from aigateway.core import Database, create_user, dumps
from aigateway.routing import admit
from aigateway.security import GatewayError
from conftest import HEADERS, PASSWORD, chat_result, configured

BODY = {'model':'qa-model','messages':[{'role':'user','content':'请回答测试问题，不包含真实数据'}],'max_tokens':64}


def test_chat_usage_charge_idempotency_and_privacy(gateway):
    g = configured(gateway)
    c,db=g['client'],g['db']
    sent=[]
    def handler(request):
        sent.append(json.loads(request.content))
        assert request.headers['authorization']=='Bearer synthetic-upstream-secret-ONLY-QA'
        return httpx.Response(200,json=chat_result())
    g['app'].state.upstream_transport=httpx.MockTransport(handler)
    r=c.post('/v1/chat/completions',json=BODY,headers={**g['headers'],'Idempotency-Key':'qa-inference-0001'})
    assert r.status_code==200,r.text
    assert sent[0]['model']=='upstream-model' and sent[0]['max_completion_tokens']==64 and sent[0]['store'] is False
    row=db.one('SELECT * FROM requests')
    assert (row['input_tokens'],row['output_tokens'],row['cached_tokens'],row['charged_micro'])==(100,20,10,135)
    assert row['status']=='succeeded'
    duplicate=c.post('/v1/chat/completions',json=BODY,headers={**g['headers'],'Idempotency-Key':'qa-inference-0001'})
    assert duplicate.status_code==409 and duplicate.json()['error']['request_id']==row['id']
    assert len(sent)==1
    assert c.post('/v1/chat/completions',json={**BODY,'max_tokens':60},headers={**g['headers'],'Idempotency-Key':'qa-inference-0001'}).status_code==409
    listings=c.get('/api/v1/keys').text+c.get('/api/v1/admin/upstreams').text+c.get('/api/v1/admin/requests').text
    assert g['token'] not in listings and 'synthetic-upstream-secret' not in listings and BODY['messages'][0]['content'] not in listings
    raw=db.path.read_bytes()
    assert g['token'].encode() not in raw and b'synthetic-upstream-secret' not in raw and BODY['messages'][0]['content'].encode() not in raw
    assert ledger_check(db)['ok']
    with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE ledger SET delta=0")


def test_missing_balance_and_all_scope_quotas(gateway):
    g=configured(gateway,balance_fen=0)
    c=g['client']
    r=c.post('/v1/chat/completions',json=BODY,headers=g['headers'])
    assert r.status_code==402
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==0
    c.post('/api/v1/admin/grants',json={'tenant_id':g['tenant'],'amount_micro':1000000,'client_ref':'grant-000001','note':'QA virtual grant'})
    for scope,pk in [('platform','global'),('tenant',g['tenant']),('user',g['user']),('application',g['application']),('key',g['key_id'])]:
        assert c.put('/api/v1/admin/policies/'+scope+'/'+pk,json={'daily_tokens':0}).status_code==200
        assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==429
        assert c.put('/api/v1/admin/policies/'+scope+'/'+pk,json={}).status_code==200
    assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==200
    assert ledger_check(g['db'])['ok']


def test_concurrent_holds_never_overspend_and_ten_way_capacity(gateway):
    g=configured(gateway,balance_fen=0)
    c,db=g['client'],g['db']
    c.post('/api/v1/admin/grants',json={'tenant_id':g['tenant'],'amount_micro':2500,'client_ref':'grant-cap-0001','note':'QA concurrent grant'})
    def reserve(_):
        try:return admit(db,g['token'],'testclient',BODY,'chat','/chat/completions')['id']
        except GatewayError as exc:return exc.status
    with ThreadPoolExecutor(max_workers=10) as executor:results=list(executor.map(reserve,range(10)))
    ids=[x for x in results if isinstance(x,str)]
    assert len(ids)==1,results
    assert all(x==402 for x in results if isinstance(x,int))
    settle(db,ids[0],status='cancelled')
    c.post('/api/v1/admin/grants',json={'tenant_id':g['tenant'],'amount_micro':1000000,'client_ref':'grant-cap-0002','note':'QA ten-way grant'})
    with ThreadPoolExecutor(max_workers=11) as executor:results=list(executor.map(reserve,range(11)))
    ids=[x for x in results if isinstance(x,str)]
    assert len(ids)==10 and results.count(429)==1,results
    for pk in ids:settle(db,pk,(100,20,10))
    assert ledger_check(db)['ok']
    assert db.one("SELECT count(*) n FROM requests WHERE status='running'")['n']==0


def test_tenant_isolation_key_rotation_and_live_revocation(gateway):
    g=configured(gateway)
    c,db=g['client'],g['db']
    user=c.post('/api/v1/admin/users',json={'username':'second','display_name':'第二租户','password':PASSWORD}).json()
    with closing(TestClient(g['app'])) as other:
        other.headers.update(HEADERS)
        assert other.post('/api/v1/auth/login',json={'username':'second','password':PASSWORD}).status_code==200
        assert other.get('/api/v1/keys',params={'tenant_id':g['tenant']}).status_code==403
        assert other.get('/api/v1/wallet',params={'tenant_id':g['tenant']}).status_code==403
        assert other.get('/api/v1/admin/upstreams').status_code==403
        assert other.delete('/api/v1/keys/'+g['key_id']).status_code==404
    rotated=c.post('/api/v1/keys/'+g['key_id']+'/rotate').json()['key']
    assert c.get('/v1/models',headers=g['headers']).status_code==401
    new_headers={'Authorization':'Bearer '+rotated}
    assert c.get('/v1/models',headers=new_headers).status_code==200
    assert c.delete('/api/v1/keys/'+g['key_id']).status_code==200
    assert c.get('/v1/models',headers=new_headers).status_code==401
    invite=c.post('/api/v1/invitations',json={'role':'member'}).json()['invitation']
    assert c.post('/api/v1/auth/register',json={'username':'invited','display_name':'成员','password':PASSWORD,'invitation':invite}).status_code==201
    assert c.post('/api/v1/auth/register',json={'username':'replay','display_name':'重放','password':PASSWORD,'invitation':invite}).status_code==403
    assert db.one("SELECT count(*) n FROM users")['n']==3


def test_streaming_responses_tools_and_compact(gateway):
    g=configured(gateway)
    c=g['client'];seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        if request.url.path.endswith('/compact'):
            return httpx.Response(200,json={'id':'cmp-qa','object':'response.compaction','output':[{'type':'compaction','encrypted_content':'synthetic-opaque-state'}],
                'usage':{'input_tokens':120,'output_tokens':10,'input_tokens_details':{'cached_tokens':0}}})
        events=[{'type':'response.created','response':{'id':'resp-qa','status':'in_progress'}},
                {'type':'response.output_item.added','output_index':0,'item':{'type':'function_call','id':'fc-qa','call_id':'call-qa','name':'echo','arguments':''}},
                {'type':'response.function_call_arguments.delta','item_id':'fc-qa','output_index':0,'delta':'{"text":"ok"}'},
                {'type':'response.completed','response':{'id':'resp-qa','status':'completed','usage':{'input_tokens':80,'output_tokens':30,'input_tokens_details':{'cached_tokens':5}}}}]
        stream=''.join('event: '+e['type']+'\r\ndata: '+json.dumps(e)+'\r\n\r\n' for e in events)
        return httpx.Response(200,content=stream,headers={'content-type':'text/event-stream'})
    g['app'].state.upstream_transport=httpx.MockTransport(handler)
    body={'model':'qa-model','input':[{'role':'user','content':'使用echo工具'}],'stream':True,
          'tools':[{'type':'function','name':'echo','parameters':{'type':'object','properties':{'text':{'type':'string'}}}}]}
    r=c.post('/v1/responses',json=body,headers={**g['headers'],'X-Gateway-Session':'qa-session'})
    assert r.status_code==200 and 'function_call_arguments.delta' in r.text and 'response.completed' in r.text
    r=c.post('/v1/responses/compact',json={'model':'qa-model','input':[{'role':'user','content':'压缩历史'}]},headers={**g['headers'],'X-Gateway-Session':'qa-session'})
    assert r.status_code==200,r.text
    assert g['db'].one('SELECT count(*) n FROM affinities')['n']==1
    assert g['db'].one("SELECT count(*) n FROM requests WHERE status='succeeded'")['n']==2
    assert seen[0]['store'] is False and 'max_output_tokens' not in seen[1]
    assert ledger_check(g['db'])['ok']


def test_chat_sse_and_unknown_recovery_reconciliation(gateway):
    g=configured(gateway)
    c,db=g['client'],g['db']
    def good(request):
        chunks=[{'id':'chatqa','choices':[{'index':0,'delta':{'content':'你好'},'finish_reason':None}]},
                {'id':'chatqa','choices':[],'usage':chat_result()['usage']}]
        return httpx.Response(200,content=''.join('data: '+json.dumps(x)+'\n\n' for x in chunks)+'data: [DONE]\n\n')
    g['app'].state.upstream_transport=httpx.MockTransport(good)
    assert '[DONE]' in c.post('/v1/chat/completions',json={**BODY,'stream':True},headers=g['headers']).text
    assert db.one('SELECT * FROM requests')['status']=='succeeded'
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,content='data: {"choices":[]}\n\n'))
    r=c.post('/v1/chat/completions',json={**BODY,'stream':True},headers=g['headers'])
    assert 'stream_interrupted' in r.text
    row=db.one("SELECT * FROM requests WHERE status='unknown'")
    assert row['charged_micro']==0
    assert c.post('/api/v1/admin/requests/'+row['id']+'/reconcile',json={'input_tokens':10,'output_tokens':5,'cached_tokens':0,'reference':'QA供应商用量凭证-0001'}).status_code==200
    assert c.post('/api/v1/admin/requests/'+row['id']+'/reconcile',json={'input_tokens':10,'output_tokens':5,'reference':'QA供应商用量凭证-0001'}).status_code==409
    pending=admit(db,g['token'],'testclient',BODY,'chat','/chat/completions')
    maintenance(db,restart=True)
    assert db.one('SELECT status FROM requests WHERE id=?',(pending['id'],))['status']=='unknown'
    maintenance(db,now=time.time()+86401)
    assert db.one('SELECT status FROM requests WHERE id=?',(pending['id'],))['status']=='released'
    assert ledger_check(db)['ok']


def test_upstream_failures_cooldown_and_payload_boundaries(gateway):
    g=configured(gateway);c=g['client'];calls=[]
    def limited(request):
        calls.append(request)
        return httpx.Response(429,json={'error':'synthetic-upstream-secret-ONLY-QA'},headers={'retry-after':'20'})
    g['app'].state.upstream_transport=httpx.MockTransport(limited)
    r=c.post('/v1/chat/completions',json=BODY,headers=g['headers'])
    assert r.status_code==429 and 'synthetic-upstream-secret' not in r.text
    assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==429
    assert len(calls)==1
    g['db'].execute('UPDATE pools SET cooldown_until=0')
    for extra in ({'store':True},{'service_tier':'priority'},{'n':2},{'tools':[{'type':'web_search'}]}):
        assert c.post('/v1/chat/completions',json={**BODY,**extra},headers=g['headers']).status_code==400
    assert c.post('/v1/chat/completions',json={**BODY,'messages':[{'role':'user','content':[{'type':'image_url','image_url':'https://bad.test'}]}]},headers=g['headers']).status_code==400
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(503,text='provider secret'))
    assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==502
    assert g['db'].one("SELECT count(*) n FROM requests WHERE status='unknown'")['n']==1
    assert ledger_check(g['db'])['ok']


def test_manual_orders_refunds_packages_and_payment_boundary(gateway):
    g=configured(gateway);c,db=g['client'],g['db']
    order=c.post('/api/v1/orders',json={'kind':'topup','amount_fen':100,'client_ref':'order-refund-0001'}).json()
    for _ in range(2):assert c.post('/api/v1/admin/orders/'+order['id']+'/approve',json={'reference':'QA人工到账凭证'}).status_code==200
    assert db.one('SELECT count(*) n FROM buckets WHERE source_id=?',(order['id'],))['n']==1
    refund={'amount_fen':50,'client_ref':'refund-once-0001','reference':'QA线下退款凭证'}
    for _ in range(2):assert c.post('/api/v1/admin/orders/'+order['id']+'/refund',json=refund).status_code==200
    assert c.post('/api/v1/admin/orders/'+order['id']+'/refund',json={**refund,'amount_fen':51}).status_code==409
    plan=c.post('/api/v1/admin/plans',json={'name':'合成套餐','price_fen':100,'credit_micro':500000,'models':['qa-model'],
             'limits':{'rpm':50,'concurrency':2},'published':True}).json()
    po=c.post('/api/v1/orders',json={'kind':'plan','plan_id':plan['id'],'client_ref':'plan-order-0001'}).json()
    assert c.post('/api/v1/admin/orders/'+po['id']+'/approve',json={'reference':'QA套餐到账凭证'}).status_code==200
    r=c.post('/v1/chat/completions',json=BODY,headers=g['headers']);assert r.status_code==200,r.text
    assert db.one("SELECT remaining FROM buckets WHERE kind='package'")['remaining']==500000-135
    db.execute("UPDATE subscriptions SET expires_at=?",(time.time()-1,))
    assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==402
    pending=c.post('/api/v1/orders',json={'kind':'topup','amount_fen':100,'client_ref':'payment-boundary-0001'}).json()
    assert c.post('/api/v1/payments/wechat/checkout',json={'order_id':pending['id']}).status_code==501
    assert c.post('/api/v1/payments/manual/checkout',json={'order_id':pending['id']}).status_code==200
    assert ledger_check(db)['ok']


def test_pool_approval_model_scope_and_ip_restrictions(gateway):
    g=configured(gateway);c=g['client']
    assert c.post('/api/v1/admin/pools',json={'name':'订阅共享','kind':'chatgpt','quota_group':'chatgpt','enabled':True}).status_code==422
    no_model=c.post('/api/v1/keys',json={'name':'禁止所有模型','application_id':g['application'],'models':[]}).json()['key']
    assert c.post('/v1/chat/completions',json=BODY,headers={'Authorization':'Bearer '+no_model}).status_code==403
    ipkey=c.post('/api/v1/keys',json={'name':'IP受限','application_id':g['application'],'ips':['192.0.2.0/24']}).json()['key']
    assert c.get('/v1/models',headers={'Authorization':'Bearer '+ipkey}).status_code==403
    c.put('/api/v1/admin/pools/'+g['pool'],json={'name':'专用池','quota_group':'qa-group','visibility':'tenant','tenant_id':g['tenant'],'enabled':True})
    assert len(c.get('/v1/models',headers=g['headers']).json()['data'])==1


def test_fifty_accounts_and_disabled_membership(gateway):
    g=configured(gateway);c,db=g['client'],g['db']
    # Account-capacity fixture uses the real schema with a precomputed password hash to avoid 49 redundant PBKDF rounds.
    template=db.one('SELECT password_hash FROM users')['password_hash']
    from aigateway.core import uid
    with db.transaction() as conn:
        for i in range(49):
            conn.execute('INSERT INTO users VALUES(?,?,?,?,0,1,?)',(uid(),'load-user-'+str(i),'并发用户'+str(i),template,time.time()))
    assert len(c.get('/api/v1/admin/users').json())==50
    assert c.post('/api/v1/admin/users',json={'username':'over-cap','display_name':'超额','password':PASSWORD}).status_code==409
    assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==200
    assert ledger_check(db)['ok']

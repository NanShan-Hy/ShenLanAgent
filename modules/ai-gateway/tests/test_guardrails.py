import asyncio
import json
import time

import httpx
import pytest

from aigateway.billing import ledger_check, maintenance, settle
from aigateway.core import dumps
from aigateway.routing import admit, validate_upstream
from aigateway.security import GatewayError
from conftest import configured, chat_result

BODY={'model':'qa-model','messages':[{'role':'user','content':'本地并发验收'}],'max_tokens':64}


def test_ten_parallel_http_requests_are_atomic(gateway):
    g=configured(gateway)
    async def scenario():
        reached=0
        all_admitted=asyncio.Event()
        release=asyncio.Event()
        async def handler(request):
            nonlocal reached
            reached+=1
            if reached==10:all_admitted.set()
            await release.wait()
            return httpx.Response(200,json=chat_result())
        g['app'].state.upstream_transport=httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=g['app']),base_url='http://testserver',trust_env=False) as client:
            tasks=[asyncio.create_task(client.post('/v1/chat/completions',json=BODY,headers=g['headers'])) for _ in range(10)]
            await asyncio.wait_for(all_admitted.wait(),10)
            assert g['db'].one("SELECT count(*) n FROM requests WHERE status='running'")['n']==10
            blocked=await client.post('/v1/chat/completions',json=BODY,headers=g['headers'])
            assert blocked.status_code==429
            release.set()
            responses=await asyncio.gather(*tasks)
            assert all(r.status_code==200 for r in responses)
    asyncio.run(scenario())
    assert g['db'].one("SELECT count(*) n FROM requests WHERE status='succeeded'")['n']==10
    assert ledger_check(g['db'])['ok']


def test_revocation_before_transmission_and_price_snapshot(gateway,monkeypatch):
    g=configured(gateway)
    calls=[]
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:calls.append(r) or httpx.Response(200,json=chat_result()))
    def revoke(*args,**kwargs):
        g['db'].execute('UPDATE api_keys SET active=0 WHERE id=?',(g['key_id'],))
    monkeypatch.setattr('aigateway.relay.validate_upstream',revoke)
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==401
    assert not calls
    assert g['db'].one('SELECT status FROM requests')['status']=='cancelled'
    g['db'].execute('UPDATE api_keys SET active=1 WHERE id=?',(g['key_id'],))
    held=admit(g['db'],g['token'],'testclient',BODY,'chat','/chat/completions')
    g['db'].execute('UPDATE models SET input_rate=100000000,output_rate=100000000,price_version=2')
    assert settle(g['db'],held['id'],(100,20,10))
    assert g['db'].one('SELECT charged_micro FROM requests WHERE id=?',(held['id'],))['charged_micro']==135
    assert ledger_check(g['db'])['ok']


def test_unknown_usage_invalid_payload_and_stale_request(gateway):
    g=configured(gateway);c,db=g['client'],g['db']
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[],'usage':{'prompt_tokens':-1,'completion_tokens':0}}))
    assert c.post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==502
    assert db.one('SELECT status,charged_micro FROM requests')=={'status':'unknown','charged_micro':0}
    assert c.post('/v1/chat/completions',content='{"model":NaN}',headers={**g['headers'],'Content-Type':'application/json'}).status_code==400
    held=admit(db,g['token'],'testclient',BODY,'chat','/chat/completions')
    db.execute('UPDATE requests SET heartbeat=? WHERE id=?',(time.time()-361,held['id']))
    maintenance(db)
    assert db.one('SELECT status FROM requests WHERE id=?',(held['id'],))['status']=='unknown'
    assert ledger_check(db)['ok']


def test_funding_and_refund_races_ledger_remains_consistent(gateway):
    from concurrent.futures import ThreadPoolExecutor
    from aigateway.billing import approve_order, refund_order
    from aigateway.models import RefundInput
    g=configured(gateway);c,db=g['client'],g['db']
    order=c.post('/api/v1/orders',json={'kind':'topup','amount_fen':100,'client_ref':'concurrent-payment-0001'}).json()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:approve_order(db,order['id'],g['user'],'QA并发人工核款凭证'),range(8)))
    assert sum(not r['already_paid'] for r in results)==1
    body=RefundInput(amount_fen=100,client_ref='concurrent-refund-0001',reference='QA并发线下退款凭证')
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:refund_order(db,order['id'],g['user'],body),range(8)))
    assert db.one('SELECT count(*) n FROM refunds WHERE order_id=?',(order['id'],))['n']==1
    assert ledger_check(db)['ok']


def test_private_pool_cannot_be_purchased_as_global_plan(gateway):
    g=configured(gateway);c=g['client']
    other=c.post('/api/v1/admin/users',json={'username':'other-pool','display_name':'另一空间','password':'local-qa-password-12345'}).json()
    assert c.put('/api/v1/admin/pools/'+g['pool'],json={'name':'另一租户专用池','quota_group':'qa-group','visibility':'tenant',
        'tenant_id':other['tenant_id'],'enabled':True}).status_code==200
    plan=c.post('/api/v1/admin/plans',json={'name':'专用套餐','price_fen':100,'credit_micro':100000,'models':['qa-model'],'published':True}).json()
    assert c.get('/api/v1/plans').json()==[]
    assert c.post('/api/v1/orders',json={'kind':'plan','plan_id':plan['id'],'client_ref':'unavailable-plan-0001'}).status_code==403
    assert c.patch('/api/v1/admin/tenants/'+g['tenant'],json={'active':False}).status_code==409


def test_production_upstream_validation_and_http_guards(gateway):
    from dataclasses import replace
    settings=replace(gateway['settings'],allow_private_upstreams=False)
    for url in ('http://example.com/v1','https://127.0.0.1/v1','https://169.254.169.254/','https://metadata.google.internal/'):
        with pytest.raises(GatewayError):validate_upstream(settings,'compatible',url)
    with pytest.raises(GatewayError):validate_upstream(settings,'openai','https://example.com/v1')
    c=gateway['client']
    assert c.post('/api/v1/tenants',json={'name':'跨站请求'},headers={'Origin':'https://untrusted.test'}).status_code==403
    assert c.post('/api/v1/tenants',json={'name':'无CSRF头'},headers={'X-Requested-With':''}).status_code==403
    assert c.post('/v1/chat/completions',content=b'{}',headers={'Content-Encoding':'gzip'}).status_code==415
    assert c.get('/api/openapi.json').status_code==200


def test_operator_internal_mode_and_application_policy_readback(gateway):
    g=configured(gateway)
    c=g['client']
    other=c.post('/api/v1/admin/users',json={'username':'internal-user','display_name':'内部用户','password':'local-qa-password-12345'}).json()
    assert c.patch('/api/v1/admin/tenants/'+other['tenant_id'],json={'billing_mode':'internal','wallet_overage':False}).status_code==200
    assert g['db'].one('SELECT billing_mode FROM tenants WHERE id=?',(other['tenant_id'],))['billing_mode']=='internal'
    assert c.patch('/api/v1/admin/tenants/'+other['tenant_id'],json={'active':'yes'}).status_code==422
    assert c.put('/api/v1/policies/application/'+g['application'],json={'rpm':5,'daily_cost':1000000}).status_code==200
    app=next(a for a in c.get('/api/v1/applications').json() if a['id']==g['application'])
    assert app['limits']=={'rpm':5,'daily_cost':1000000}


def test_failed_terminal_preserves_metering_without_provider_error_leak(gateway):
    g=configured(gateway)
    data={'type':'response.failed','response':{'id':'response-failure-qa','status':'failed',
          'error':{'message':'synthetic-upstream-secret-ONLY-QA'},
          'usage':{'input_tokens':10,'output_tokens':1,'input_tokens_details':{'cached_tokens':0}}}}
    g['app'].state.upstream_transport=httpx.MockTransport(lambda request:httpx.Response(200,
        content='event: response.failed\ndata: '+json.dumps(data)+'\n\n',headers={'content-type':'text/event-stream'}))
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'故障合成请求','stream':True},headers=g['headers'])
    assert 'synthetic-upstream-secret' not in r.text
    assert 'upstream_incomplete' in r.text
    row=g['db'].one('SELECT status,charged_micro FROM requests')
    assert row=={'status':'failed','charged_micro':12}
    assert ledger_check(g['db'])['ok']


def test_historical_model_identity_is_immutable(gateway):
    g=configured(gateway)
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==200
    g['db'].execute("UPDATE models SET alias='renamed-model',upstream_model='changed-model',input_rate=99999999,price_version=2")
    assert g['client'].get('/api/v1/usage').json()['items'][0]['model']=='qa-model'
    assert g['client'].get('/api/v1/admin/requests').json()['items'][0]['model']=='qa-model'
    row=g['db'].one('SELECT price,charged_micro FROM requests')
    assert json.loads(row['price'])['upstream_model']=='upstream-model'
    assert row['charged_micro']==135

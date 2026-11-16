from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aigateway.app import create_app
from aigateway.core import Database, Settings, create_user

PASSWORD = 'local-qa-password-12345'
HEADERS = {'X-Requested-With': 'AIGateway'}


def chat_result():
    return {'id':'chatcmpl-qa', 'object':'chat.completion', 'created': 1, 'model':'upstream-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':'本地协议回答'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
                     'prompt_tokens_details':{'cached_tokens':10}}}


@pytest.fixture
def gateway(tmp_path):
    settings = Settings(tmp_path/'data', tmp_path/'config/master.key', public_url='http://testserver',
                        allow_private_upstreams=True, min_free_bytes=0)
    db = Database(settings)
    db.initialize()
    with db.transaction() as c:
        admin_id, tenant_id, application_id = create_user(c, 'admin', '运营管理员', PASSWORD, admin=True)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=chat_result()))
    app = create_app(settings, upstream_transport=transport)
    with TestClient(app) as client:
        client.headers.update(HEADERS)
        assert client.post('/api/v1/auth/login', json={'username':'admin','password':PASSWORD}).status_code == 200
        yield {'app':app,'client':client,'db':db,'settings':settings,'tenant':tenant_id,'user':admin_id,'application':application_id}


def configured(g, *, balance_fen=10000, concurrency=10):
    c = g['client']
    pool = c.post('/api/v1/admin/pools', json={'name':'QA池','quota_group':'qa-group','enabled':True,
                  'rpm':1000,'tpm':10000000,'concurrency':concurrency})
    assert pool.status_code == 201, pool.text
    pool_id = pool.json()['id']
    upstream = c.post('/api/v1/admin/upstreams', json={'name':'QA上游','pool_id':pool_id,'provider':'compatible',
                  'base_url':'https://provider.test/v1','api_key':'synthetic-upstream-secret-ONLY-QA','enabled':True,
                  'concurrency':concurrency})
    assert upstream.status_code == 201, upstream.text
    model = c.post('/api/v1/admin/models',json={'alias':'qa-model','pool_id':pool_id,'provider':'compatible',
                  'upstream_model':'upstream-model','protocols':['chat','responses','compact'],'enabled':True,
                  'input_rate':1000000,'cache_rate':500000,'output_rate':2000000})
    assert model.status_code == 201, model.text
    key = c.post('/api/v1/keys',json={'name':'QA Key','application_id':g['application']})
    assert key.status_code == 201,key.text
    if balance_fen:
        order = c.post('/api/v1/orders',json={'kind':'topup','amount_fen':balance_fen,'client_ref':'qa-funding-0001'})
        assert order.status_code == 201,order.text
        result = c.post('/api/v1/admin/orders/'+order.json()['id']+'/approve',json={'reference':'本地合成核款凭证，不是真实收款'})
        assert result.status_code == 200,result.text
    c.put('/api/v1/admin/policies/user/'+g['user'],json={'rpm':1000,'tpm':10000000,'concurrency':concurrency,'daily_tokens':10000000})
    return {**g,'pool':pool_id,'upstream':upstream.json()['id'],'model':model.json()['id'],
            'key_id':key.json()['id'],'token':key.json()['key'],'headers':{'Authorization':'Bearer '+key.json()['key']}}


def drop_v6(c):
    # Construct an actual old schema, rather than only changing its version label.
    for table in ('conversation_current','conversation_turns','conversations','gateway_files','account_bindings','binding_events','model_media','conversation_policies','managed_chunks','managed_objects','managed_settings'):
        c.execute('DROP TABLE IF EXISTS '+table)
    columns=[r[1] for r in c.execute('PRAGMA table_info(request_audits)')]
    for name in ('conversation_id','turn_id'):
        if name in columns:c.execute('ALTER TABLE request_audits DROP COLUMN '+name)

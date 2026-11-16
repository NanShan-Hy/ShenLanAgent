import json
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from aigateway.account_pool_api import PREFIX
from aigateway.billing import ledger_check
from aigateway.core import create_user, private_write
from aigateway.routing import validate_upstream
from aigateway.security import GatewayError
from conftest import HEADERS, PASSWORD, chat_result
from test_clients import response_stream

CLIENT='local-client-key-only-QA-1234567890'
MANAGEMENT='local-management-key-only-QA-1234567890'


@pytest.fixture
def pool(gateway,tmp_path):
    g=gateway
    g['settings'].allow_private_upstreams=False
    config=tmp_path/'pool/connector.json'
    private_write(config,json.dumps({'base_url':'http://127.0.0.1:8095/v1','client_key':CLIENT,'management_key':MANAGEMENT}))
    g['settings'].account_pool_config=config
    control={'files':[{'name':'codex-qa.json','provider':'codex','label':'QA GPT账号','status':'active','disabled':False,
        'access_token':'DO-NOT-ECHO-ACCESS','refresh_token':'DO-NOT-ECHO-REFRESH','metadata':{'secret':'DO-NOT-ECHO-METADATA'}},
        {'name':'other.json','provider':'claude','status':'active'}],
        'calls':[],'exchanges':0,'status':'wait','error':None,'inference':[],'oauth_error':'SENSITIVE-BACKEND-ERROR','saved':True,'adopted':True}
    def handler(request):
        path=request.url.path;control['calls'].append((request.method,path))
        if control['error']:
            return httpx.Response(503,json={'error':'SENSITIVE-BACKEND-ERROR '+MANAGEMENT})
        if path.startswith('/v8/management'):
            assert request.headers['Authorization']=='Bearer '+MANAGEMENT
            if path.endswith('/credentials'):
                return httpx.Response(200,json={'files':control['files']})
            if path.endswith('/oauth/auth-url'):
                return httpx.Response(200,json={'url':'https://auth.openai.com/oauth/authorize?'+urlencode({
                    'state':'QA-STATE','redirect_uri':'http://localhost:1455/auth/callback',
                    'code_challenge_method':'S256','code_challenge':'c'*43,'client_id':'public-client-qa'}),'state':'QA-STATE'})
            if path.endswith('/oauth/callback'):
                control['exchanges']+=1
                assert json.loads(request.content)=={'provider':'codex','state':'QA-STATE','code':control.get('callback_code','QA-code')}
                return httpx.Response(200,json={'status':'ok'})
            if path.endswith('/oauth/status'):return httpx.Response(200,json={'status':control['status'],'error':control['oauth_error'],'saved':control['saved'],'adopted':control['adopted'],'auth_id':'codex-qa.json','adoption_error':control.get('adoption_error','')})
            if path.endswith('/oauth/session'):return httpx.Response(200,json={'status':'ok'})
            if path.endswith('/credentials/status'):
                body=json.loads(request.content)
                assert body['name']=='codex-qa.json'
                control['files'][0]['disabled']=body['disabled']
                return httpx.Response(200,json={'status':'ok','secret':MANAGEMENT})
            if path.endswith('/credentials/refresh'):
                assert json.loads(request.content)=={'name':'codex-qa.json'}
                return httpx.Response(200,json={'ok':True,'auth':{'access_token':'DO-NOT-ECHO-ACCESS'}})
        assert request.headers['Authorization']=='Bearer '+CLIENT
        if path=='/v1/models':return httpx.Response(200,json={'data':[{'id':'gpt-qa-pool'}]})
        if control.get('inference_limit'):
            return httpx.Response(control.get('inference_status',429),headers={'Retry-After':'3600'},json={'error':{'code':'usage_limit_reached'}})
        assert request.headers['x-session-id'].startswith('gw-')
        control['inference'].append(dict(request.headers))
        if path=='/v1/chat/completions':return httpx.Response(200,json=chat_result())
        if path=='/v1/responses':return response_stream('POOL_RESPONSE_OK')
        raise AssertionError(path)
    g['app'].state.upstream_transport=httpx.MockTransport(handler)
    yield {**g,'control':control}


def bootstrap(g,*,platform=False):
    r=g['client'].post(PREFIX+'/bootstrap',json={'tenant_id':g['tenant'],'platform_shared':platform,'sharing_consent':True})
    assert r.status_code==201,r.text
    return r.json()


def begin(g):
    r=g['client'].post(PREFIX+'/authorize',json={'sharing_consent':True});assert r.status_code==200,r.text
    return r.json()


def test_pool_status_has_concrete_steps_and_does_not_echo_credentials(pool):
    g=pool;r=g['client'].get(PREFIX+'/status');assert r.status_code==200
    data=r.json();assert data['online'] and not data['ready']
    assert [s['id'] for s in data['steps']]==['backend','pool','accounts','catalog','models']
    assert len(data['accounts'])==1 and data['accounts'][0]['label']=='QA GPT账号'
    assert all(s not in r.text for s in (CLIENT,MANAGEMENT,'DO-NOT-ECHO','codex-qa.json'))
    g['control']['error']=True
    r=g['client'].get(PREFIX+'/status')
    assert not r.json()['online'] and r.json()['error_code']=='account_pool_backend_503'
    assert MANAGEMENT not in r.text and 'SENSITIVE' not in r.text


def test_pool_bootstrap_is_explicit_scoped_idempotent_and_loopback_exception_is_exact(pool):
    g=pool
    assert g['client'].post(PREFIX+'/bootstrap',json={'tenant_id':g['tenant']}).status_code==400
    result=bootstrap(g);again=bootstrap(g)
    assert again['created'] is False and again['pool_id']==result['pool_id']
    assert g['db'].one("SELECT count(*) n FROM upstreams WHERE provider='account_pool'")['n']==1
    assert g['db'].one('SELECT visibility,tenant_id FROM pools')=={'visibility':'tenant','tenant_id':g['tenant']}
    validate_upstream(g['settings'],'account_pool','http://127.0.0.1:8095/v1',resolve=True)
    for provider,url in [('account_pool','http://127.0.0.1:8090/v1'),('account_pool','http://169.254.169.254/v1'),
                         ('compatible','http://127.0.0.1:8095/v1'),('account_pool','http://127.0.0.1:8095/v1?x=y')]:
        with pytest.raises(GatewayError):validate_upstream(g['settings'],provider,url,resolve=True)
    assert not g['settings'].allow_private_upstreams


def test_managed_model_requires_correct_provider(pool):
    g=pool;b=bootstrap(g)
    r=g['client'].post('/api/v1/admin/models',json={'alias':'wrong','pool_id':b['pool_id'],'provider':'compatible','upstream_model':'gpt-qa-pool'})
    assert r.status_code==400 and r.json()['error']['code']=='account_pool_model_provider'


def test_authorization_manual_callback_is_bound_consumed_and_completion_is_polled(pool):
    g=pool
    assert g['client'].post(PREFIX+'/authorize',json={}).status_code==400
    flow=begin(g);assert flow['callback_mode']=='auto_with_paste_fallback'
    for state in ('wrong','汉字'):
        bad='http://localhost:1455/auth/callback?'+urlencode({'state':state,'code':'QA-code'})
        assert g['client'].post(PREFIX+'/authorizations/'+flow['id']+'/callback',json={'redirect_url':bad}).status_code==400
    assert g['control']['exchanges']==0
    url='http://localhost:1455/auth/callback?state=QA-STATE&code=QA-code'
    r=g['client'].post(PREFIX+'/authorizations/'+flow['id']+'/callback',json={'redirect_url':url})
    assert r.status_code==200 and r.json()['token_exchange_complete'] is False
    assert g['client'].post(PREFIX+'/authorizations/'+flow['id']+'/callback',json={'redirect_url':url}).status_code==409
    assert g['control']['exchanges']==1
    assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()['status']=='wait'
    g['control']['status']='ok'
    assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()['status']=='ok'


def test_callback_rejects_foreign_redirect_and_raw_backend_errors_are_hidden(pool):
    g=pool;flow=begin(g)
    url='https://attacker.test/auth/callback?state=QA-STATE&code=QA-code'
    assert g['client'].post(PREFIX+'/authorizations/'+flow['id']+'/callback',json={'redirect_url':url}).status_code==400
    g['control']['status']='error'
    r=g['client'].get(PREFIX+'/authorizations/'+flow['id'])
    assert r.json()['error_code']=='account_pool_authorization_failed' and 'SENSITIVE' not in r.text


@pytest.mark.parametrize('action',['logout','login'])
def test_ended_session_cannot_complete_pool_authorization(pool,action):
    g=pool;flow=begin(g)
    if action=='logout':g['client'].post('/api/v1/auth/logout',json={})
    else:g['client'].post('/api/v1/auth/login',json={'username':'admin','password':PASSWORD})
    r=g['client'].post(PREFIX+'/authorizations/'+flow['id']+'/callback',json={'redirect_url':'http://localhost:1455/auth/callback?state=QA-STATE&code=QA-code'})
    assert r.status_code in (401,404) and g['control']['exchanges']==0


def test_pool_administration_is_not_available_to_member_or_inference_keys(pool):
    g=pool
    with g['db'].transaction() as c:user,tenant,_=create_user(c,'member','普通用户',PASSWORD)
    with closing(TestClient(g['app'])) as client:
        client.headers.update(HEADERS)
        client.post('/api/v1/auth/login',json={'username':'member','password':PASSWORD})
        assert client.get(PREFIX+'/status').status_code==403
        assert client.post(PREFIX+'/bootstrap',json={'tenant_id':tenant,'sharing_consent':True}).status_code==403
    with closing(TestClient(g['app'])) as client:
        assert client.get(PREFIX+'/status',headers={'Authorization':'Bearer '+CLIENT}).status_code==401


def test_batch_actions_return_each_result_without_exporting_credentials(pool):
    g=pool;pk=g['client'].get(PREFIX+'/status').json()['accounts'][0]['id']
    for action in ('disable','enable','refresh'):
        r=g['client'].post(PREFIX+'/accounts/batch',json={'ids':[pk,'missing'],'action':action})
        assert r.status_code==200 and r.json()['results']==[{'id':pk,'ok':True},{'id':'missing','ok':False,'error_code':'account_pool_account_not_found'}]
        assert all(s not in r.text for s in (CLIENT,MANAGEMENT,'DO-NOT-ECHO'))
    assert g['client'].post(PREFIX+'/accounts/batch',json={'ids':[pk,pk],'action':'disable'}).status_code==400
    assert g['client'].post(PREFIX+'/accounts/batch',json={'ids':['x']*21,'action':'refresh'}).status_code==422


def test_expired_and_cancelled_authorizations_cannot_be_replayed(pool):
    g=pool;flow=begin(g)
    assert g['client'].delete(PREFIX+'/authorizations/'+flow['id']).json()['cancelled']
    assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).status_code==404
    flow=begin(g);g['app'].state.account_pool_flows[flow['id']]['expires_at']=0
    assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).status_code==410


def test_pool_model_discovery_platform_key_and_chat_responses_billing(pool):
    g=pool;client=g['client'];b=bootstrap(g)
    probe=client.post('/api/v1/admin/upstreams/'+b['upstream_id']+'/probe',json={})
    assert probe.status_code==200 and probe.json()['status']=='ok',probe.text
    r=client.post('/api/v1/admin/models',json={'alias':'pool-gpt','pool_id':b['pool_id'],'provider':'account_pool',
        'upstream_model':'gpt-qa-pool','protocols':['chat','responses'],'enabled':True,
        'input_rate':1000000,'cache_rate':500000,'output_rate':2000000})
    assert r.status_code==201,r.text
    key=client.post('/api/v1/keys',json={'name':'业务系统Key','application_id':g['application'],'models':['pool-gpt']}).json()
    order=client.post('/api/v1/orders',json={'kind':'topup','amount_fen':1000,'client_ref':'pool-qa-funding'}).json()
    assert client.post('/api/v1/admin/orders/'+order['id']+'/approve',json={'reference':'合成验收，不是真实收款'}).status_code==200
    assert client.get(PREFIX+'/status').json()['ready']
    headers={'Authorization':'Bearer '+key['key'],'X-Gateway-Session':'client-project-one'}
    chat=client.post('/v1/chat/completions',headers=headers,json={'model':'pool-gpt','messages':[{'role':'user','content':'Synthetic QA only'}]})
    assert chat.status_code==200,chat.text
    response=client.post('/v1/responses',headers=headers,json={'model':'pool-gpt','input':'Synthetic QA only','stream':True})
    assert response.status_code==200 and 'response.completed' in response.text
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==2
    assert ledger_check(g['db'])['ok']
    assert CLIENT not in chat.text and MANAGEMENT not in response.text
    assert all(h['authorization']=='Bearer '+CLIENT and h['x-session-id']!='client-project-one' for h in g['control']['inference'])


def test_connector_private_file_and_unconfigured_status_are_checked(pool):
    g=pool;g['settings'].account_pool_config.chmod(0o644)
    assert g['client'].get(PREFIX+'/status').json()['error_code']=='account_pool_configuration'
    g['settings'].account_pool_config=None
    data=g['client'].get(PREFIX+'/status').json()
    assert not data['configured'] and data['error_code']=='account_pool_not_configured'


def test_maintenance_blocks_account_management_without_sending_upstream(pool):
    g=pool;calls=len(g['control']['calls'])
    private_write(g['settings'].data_dir/'maintenance','isolated QA maintenance')
    r=g['client'].post(PREFIX+'/authorize',json={'sharing_consent':True})
    assert r.status_code==503 and r.json()['error']['code']=='maintenance'
    assert len(g['control']['calls'])==calls


def test_model_batch_is_atomic_and_versions_prices(pool):
    g=pool;b=bootstrap(g)
    assert g['client'].post('/api/v1/admin/upstreams/'+b['upstream_id']+'/probe',json={}).status_code==200
    values={'model_ids':['gpt-qa-pool'],'input_rate':1000000,'cache_rate':500000,'output_rate':2000000}
    first=g['client'].post(PREFIX+'/models',json=values)
    assert first.status_code==200 and first.json()['models'][0]['alias']=='gpt/gpt-qa-pool'
    assert g['db'].one('SELECT price_version FROM models')['price_version']==1
    values['output_rate']=3000000
    assert g['client'].post(PREFIX+'/models',json=values).status_code==200
    assert g['db'].one('SELECT price_version FROM models')['price_version']==2
    values['model_ids']=['gpt-qa-pool','not-in-catalog'];values['output_rate']=4000000
    assert g['client'].post(PREFIX+'/models',json=values).status_code==409
    assert g['db'].one('SELECT price_version,output_rate FROM models')=={'price_version':2,'output_rate':3000000}


def test_restored_gateway_cannot_manage_the_original_account_pool(pool,tmp_path):
    from aigateway.app import create_app
    from aigateway.cli import backup,verify
    from aigateway.core import Settings
    g=pool;bootstrap(g)
    snapshot=backup(g['db'],root=tmp_path/'snapshots')
    destination=tmp_path/'restore'
    report=verify(snapshot,destination)
    assert report['encrypted_records_verified']==1
    restored=Settings.load(destination/'config/settings.json')
    assert restored.account_pool_config is None
    assert json.loads((snapshot/'config/settings.json').read_text())['account_pool_config']==str(g['settings'].account_pool_config)


@pytest.mark.parametrize('status_code',[401,403,429])
def test_single_account_limit_does_not_freeze_independent_accounts_in_bridge(pool,status_code):
    g=pool;b=bootstrap(g);c=g['client']
    assert c.post('/api/v1/admin/models',json={'alias':'pool-limit','pool_id':b['pool_id'],'provider':'account_pool',
        'upstream_model':'gpt-qa-pool','protocols':['chat'],'enabled':True,'input_rate':1,'cache_rate':1,'output_rate':1}).status_code==201
    key=c.post('/api/v1/keys',json={'name':'限额隔离QA','application_id':g['application']}).json()['key']
    order=c.post('/api/v1/orders',json={'kind':'topup','amount_fen':100,'client_ref':'pool-limit-qa-funding'}).json()
    assert c.post('/api/v1/admin/orders/'+order['id']+'/approve',json={'reference':'合成限额隔离验证，不是真实收款'}).status_code==200
    body={'model':'pool-limit','messages':[{'role':'user','content':'Synthetic independent task'}]}
    headers={'Authorization':'Bearer '+key}
    g['control']['inference_limit']=True;g['control']['inference_status']=status_code
    before=len(g['control']['calls'])
    assert c.post('/v1/chat/completions',headers=headers,json=body).status_code==(429 if status_code==429 else 502)
    assert len(g['control']['calls'])==before+1
    assert g['db'].one('SELECT cooldown_until FROM pools WHERE id=?',(b['pool_id'],))['cooldown_until']==0
    assert g['db'].one('SELECT state FROM upstream_runtime WHERE upstream_id=?',(b['upstream_id'],))['state']!='blocked'
    g['control']['inference_limit']=False
    assert c.post('/v1/chat/completions',headers=headers,json=body).status_code==200
    assert ledger_check(g['db'])['ok']

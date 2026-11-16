import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aigateway.billing import ledger_check
from aigateway.cli import backup, verify
from aigateway.core import Database, Settings, SCHEMA, create_user, dumps, uid
from conftest import HEADERS, PASSWORD, chat_result, configured
from live import serve

BODY={'model':'qa-model','messages':[{'role':'user','content':'连接诊断合成输入'}],'max_tokens':64}


def extra_upstream(g):
    result=g['client'].post('/api/v1/admin/upstreams',json={'name':'备用上游','pool_id':g['pool'],'provider':'compatible',
        'base_url':'https://backup.test/v1','api_key':'synthetic-backup-ONLY-QA','enabled':True,'concurrency':10})
    assert result.status_code==201,result.text
    return result.json()['id']


def test_probe_catalog_is_bounded_scoped_and_not_generation(gateway):
    g=configured(gateway);c,db=g['client'],g['db'];seen=[]
    def handler(request):
        seen.append((request.method,request.url.path))
        return httpx.Response(200,json={'object':'list','data':[{'id':'vendor/model-A','private_metadata':'NOT-SAVED'},{'id':'model-B'},{'id':'model-B'}]})
    g['app'].state.upstream_transport=httpx.MockTransport(handler)
    result=c.post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={})
    assert result.status_code==200,result.text
    assert result.json()['status']=='ok' and result.json()['models']==['model-B','vendor/model-A']
    assert seen==[('GET','/v1/models')]
    assert db.one('SELECT count(*) n FROM requests')['n']==0
    assert db.one('SELECT count(*) n FROM models')['n']==1
    assert 'private_metadata' not in result.text
    catalog=c.get('/api/v1/admin/upstreams/'+g['upstream']+'/catalog').json()
    assert catalog['models']==['model-B','vendor/model-A']
    assert c.post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}).status_code==429
    assert ledger_check(db)['ok']
    # Metadata and probe success do not claim inference capability.
    status=c.get('/api/v1/admin/pool-status').json()
    assert status['upstreams'][0]['runtime']['last_probe']['status']=='ok'
    assert status['upstreams'][0]['runtime']['last_success_at'] is None
    other=c.post('/api/v1/admin/users',json={'username':'probe-user','display_name':'普通用户','password':PASSWORD}).json()
    with closing(TestClient(g['app'])) as member:
        member.headers.update(HEADERS)
        member.post('/api/v1/auth/login',json={'username':'probe-user','password':PASSWORD})
        assert member.post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}).status_code==403
        assert member.get('/api/v1/admin/pool-status').status_code==403


@pytest.mark.parametrize('payload',[
    {'error':'synthetic-upstream-secret-ONLY-QA'},
    {'data':[{'id':'bad\nmodel'}]},
    {'data':[{'id':'x'*121}]},
    {'data':[{'id':str(i)} for i in range(1001)]},
])
def test_invalid_catalog_never_publishes_or_echoes_provider_body(gateway,payload):
    g=configured(gateway)
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload))
    r=g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={})
    assert r.status_code==200 and r.json()['status']=='failed'
    assert r.json()['error_code']=='catalog_response_invalid'
    assert 'synthetic-upstream-secret' not in r.text
    assert not g['client'].get('/api/v1/admin/upstreams/'+g['upstream']+'/catalog').json()['models']


def test_probe_revision_fence_and_authorization_recheck(gateway,monkeypatch):
    g=configured(gateway)
    def changed(request):
        g['db'].execute('UPDATE upstreams SET revision=revision+1 WHERE id=?',(g['upstream'],))
        return httpx.Response(200,json={'data':[{'id':'old-revision-model'}]})
    g['app'].state.upstream_transport=httpx.MockTransport(changed)
    assert g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}).status_code==409
    assert g['db'].one('SELECT count(*) n FROM upstream_catalog')['n']==0
    assert g['db'].one('SELECT status FROM upstream_probes')['status']=='superseded'
    g['db'].execute('UPDATE upstream_probes SET created_at=?',(time.time()-11,))
    called=[]
    def revoke(*args,**kwargs):
        g['db'].execute('UPDATE users SET active=0 WHERE id=?',(g['user'],))
    monkeypatch.setattr('aigateway.diagnostics.validate_upstream',revoke)
    g['app'].state.upstream_transport=httpx.MockTransport(lambda request:called.append(request) or httpx.Response(200,json={'data':[]}))
    assert g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}).status_code==403
    assert not called


def test_safe_failover_has_one_hold_and_records_two_attempts(gateway):
    g=configured(gateway);extra_upstream(g)
    selected=[]
    def handler(request):
        selected.append(request.url.host)
        if len(selected)==1:raise httpx.ConnectError('synthetic-upstream-secret-ONLY-QA',request=request)
        return httpx.Response(200,json=chat_result())
    g['app'].state.upstream_transport=httpx.MockTransport(handler)
    response=g['client'].post('/v1/chat/completions',json=BODY,headers={**g['headers'],'Idempotency-Key':'one-logical-request'})
    assert response.status_code==200,response.text
    assert len(selected)==2 and selected[0]!=selected[1]
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==1
    request=g['db'].one('SELECT * FROM requests')
    assert request['charged_micro']==135
    attempts=g['client'].get('/api/v1/admin/requests/'+request['id']+'/attempts').json()
    assert [r['status'] for r in attempts]==['failed','succeeded']
    assert [r['attempt_no'] for r in attempts]==[1,2]
    assert 'synthetic-upstream-secret' not in json.dumps(attempts)
    assert g['db'].one("SELECT count(*) n FROM ledger WHERE kind='hold'")['n']==1
    assert g['client'].post('/v1/chat/completions',json=BODY,headers={**g['headers'],'Idempotency-Key':'one-logical-request'}).status_code==409
    assert len(selected)==2 and ledger_check(g['db'])['ok']


@pytest.mark.parametrize('failure',['write','read','server'])
def test_uncertain_transmission_is_not_replayed(gateway,failure):
    g=configured(gateway);extra_upstream(g);calls=[]
    def handler(request):
        calls.append(request)
        if failure=='write':raise httpx.WriteError('private upstream detail',request=request)
        if failure=='read':raise httpx.ReadTimeout('private upstream detail',request=request)
        return httpx.Response(503,text='synthetic-upstream-secret-ONLY-QA')
    g['app'].state.upstream_transport=httpx.MockTransport(handler)
    r=g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers'])
    assert r.status_code==502 and len(calls)==1
    row=g['db'].one('SELECT * FROM requests')
    assert row['status']=='unknown' and row['charged_micro']==0
    assert g['db'].one('SELECT count(*) n FROM upstream_attempts')['n']==1
    assert 'private upstream' not in r.text and 'synthetic-upstream-secret' not in r.text
    assert ledger_check(g['db'])['ok']


def test_circuit_opens_half_open_is_single_and_real_success_recovers(gateway):
    g=configured(gateway)
    g['app'].state.upstream_transport=httpx.MockTransport(lambda request:httpx.Response(503,text='outage'))
    for _ in range(3):
        assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==502
    state=g['db'].one('SELECT * FROM upstream_runtime WHERE upstream_id=?',(g['upstream'],))
    assert state['state']=='open' and state['failures']==3
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==503
    # Catalog success cannot reset an inference outage.
    g['app'].state.upstream_transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[]}))
    assert g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}).json()['status']=='ok'
    assert g['db'].one('SELECT state FROM upstream_runtime')['state']=='open'
    g['db'].execute('UPDATE upstream_runtime SET retry_at=?',(time.time()-1,))
    async def scenario():
        admitted=asyncio.Event();release=asyncio.Event();calls=[]
        async def recovered(request):
            calls.append(request);admitted.set();await release.wait()
            return httpx.Response(200,json=chat_result())
        g['app'].state.upstream_transport=httpx.MockTransport(recovered)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=g['app']),base_url='http://testserver',trust_env=False) as client:
            first=asyncio.create_task(client.post('/v1/chat/completions',json=BODY,headers=g['headers']))
            await asyncio.wait_for(admitted.wait(),5)
            assert g['db'].one('SELECT state FROM upstream_runtime')['state']=='half_open'
            blocked=await client.post('/v1/chat/completions',json=BODY,headers=g['headers'])
            assert blocked.status_code==503 and len(calls)==1
            release.set();assert (await first).status_code==200
    asyncio.run(scenario())
    state=g['db'].one('SELECT * FROM upstream_runtime')
    assert state['state']=='closed' and state['failures']==0
    assert ledger_check(g['db'])['ok']


def test_auth_block_and_group_cooldown_survive_manual_circuit_reset(gateway):
    g=configured(gateway)
    g['app'].state.upstream_transport=httpx.MockTransport(lambda request:httpx.Response(401,text='sensitive credential'))
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==502
    assert g['db'].one('SELECT state FROM upstream_runtime')['state']=='blocked'
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==503
    future=time.time()+10000
    g['db'].execute('UPDATE pools SET cooldown_until=?',(future,))
    assert g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/reset-circuit',json={}).status_code==200
    assert g['db'].one('SELECT cooldown_until FROM pools')['cooldown_until']==future
    assert g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}).status_code==429
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==429


def test_group_rpm_counts_safe_retry_as_another_physical_attempt(gateway):
    g=configured(gateway);extra_upstream(g)
    g['db'].execute('UPDATE pools SET rpm=1')
    calls=[]
    def failed(request):
        calls.append(request);raise httpx.ConnectError('connection refused',request=request)
    g['app'].state.upstream_transport=httpx.MockTransport(failed)
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==502
    assert len(calls)==1
    assert g['db'].one('SELECT charged_micro FROM requests')['charged_micro']==0
    assert ledger_check(g['db'])['ok']


def test_responses_affinity_never_migrates_after_connection_failure(gateway):
    g=configured(gateway);extra_upstream(g);calls=[]
    def failure(request):
        calls.append(request);raise httpx.ConnectError('refused',request=request)
    g['app'].state.upstream_transport=httpx.MockTransport(failure)
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'same session'},headers=g['headers'])
    assert r.status_code==502 and len(calls)==1
    assert g['db'].one('SELECT count(*) n FROM upstream_attempts')['n']==1
    assert g['db'].one('SELECT count(*) n FROM affinities')['n']==1


def test_streaming_chat_never_switches_channels(gateway):
    g=configured(gateway);extra_upstream(g);calls=[]
    def failure(request):
        calls.append(request);raise httpx.ConnectTimeout('connect timeout',request=request)
    g['app'].state.upstream_transport=httpx.MockTransport(failure)
    assert g['client'].post('/v1/chat/completions',json={**BODY,'stream':True},headers=g['headers']).status_code==502
    assert len(calls)==1 and ledger_check(g['db'])['ok']


@pytest.mark.parametrize('fault',['unauthorized','rate_limited','oversized','deadline'])
def test_probe_failures_are_bounded_and_separate_from_inference(gateway,monkeypatch,fault):
    g=configured(gateway)
    async def failed(request):
        if fault=='deadline':
            await asyncio.sleep(.5)
        if fault=='oversized':
            return httpx.Response(200,content=b'x'*(1024**2+1))
        return httpx.Response(429 if fault=='rate_limited' else 401,
                              headers={'retry-after':'10000'},text='synthetic-secret-never-log')
    monkeypatch.setattr('aigateway.diagnostics.PROBE_TIMEOUT',.1 if fault=='deadline' else 10)
    g['app'].state.upstream_transport=httpx.MockTransport(failed)
    r=g['client'].post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={})
    assert r.status_code==200 and r.json()['status']=='failed'
    assert 'synthetic-secret' not in r.text
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==0
    assert g['db'].one('SELECT state FROM upstream_runtime')['state']=='closed'
    assert not g['client'].get('/api/v1/admin/upstreams/'+g['upstream']+'/catalog').json()['models']
    if fault=='rate_limited':
        assert g['db'].one('SELECT cooldown_until FROM pools')['cooldown_until']>time.time()+9900


def test_probe_leases_limit_overlap_and_cancel_cleanly(gateway):
    g=configured(gateway);second=extra_upstream(g)
    third=g['client'].post('/api/v1/admin/upstreams',json={'name':'第三上游','pool_id':g['pool'],
        'provider':'compatible','base_url':'https://third.test/v1','api_key':'synthetic-third-ONLY-QA'}).json()['id']
    async def scenario():
        reached=asyncio.Event();release=asyncio.Event();calls=[]
        async def held(request):
            calls.append(request)
            if len(calls)==2:reached.set()
            await release.wait()
            return httpx.Response(200,json={'data':[]})
        g['app'].state.upstream_transport=httpx.MockTransport(held)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=g['app']),base_url='http://testserver',
                                    cookies=dict(g['client'].cookies),headers=HEADERS,trust_env=False) as client:
            first=asyncio.create_task(client.post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={}))
            other=asyncio.create_task(client.post('/api/v1/admin/upstreams/'+second+'/probe',json={}))
            await asyncio.wait_for(reached.wait(),5)
            assert (await client.post('/api/v1/admin/upstreams/'+g['upstream']+'/probe',json={})).status_code==409
            assert (await client.post('/api/v1/admin/upstreams/'+third+'/probe',json={})).status_code==429
            first.cancel()
            with pytest.raises(asyncio.CancelledError):await first
            release.set();assert (await other).json()['status']=='ok'
    asyncio.run(scenario())
    assert [r['status'] for r in g['db'].all('SELECT status FROM upstream_probes ORDER BY created_at')]==['cancelled','ok']
    assert g['db'].one('SELECT count(*) n FROM upstream_runtime WHERE probe_id IS NOT NULL')['n']==0


def test_schema_one_upgrade_preserves_finance_and_restores_old_snapshot(gateway,tmp_path):
    from aigateway.core import digest, private_write
    from aigateway.routing import admit
    g=configured(gateway)
    assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==200
    admit(g['db'],g['token'],'testclient',BODY,'chat','/chat/completions')
    snapshot=backup(g['db'],source=Path(__file__).resolve().parents[1],root=tmp_path/'snapshots')
    legacy=snapshot/'data/gateway.sqlite3'
    with sqlite3.connect(legacy) as c:
        from conftest import drop_v6
        drop_v6(c)
        for table in ('request_capture_chunks','request_audits','request_recording_settings','chatgpt_oauth_attempts','chatgpt_usage_state','chatgpt_accounts','chatgpt_integration',
                      'upstream_catalog','upstream_probes','upstream_attempts','upstream_runtime'):
            c.execute('DROP TABLE '+table)
        c.execute('UPDATE schema_version SET version=1')
        before={table:c.execute('SELECT * FROM '+table+' ORDER BY id').fetchall()
                for table in ('requests','ledger','buckets','orders','subscriptions')}
    manifest=json.loads((snapshot/'manifest.json').read_text())
    manifest['sha256']['data/gateway.sqlite3']=digest(legacy.read_bytes())
    manifest['version']='0.1.0'
    private_write(snapshot/'manifest.json',json.dumps(manifest))
    # A direct additive migration must not settle, rewrite, or reinterpret financial history.
    direct=tmp_path/'direct';direct.mkdir()
    with sqlite3.connect(legacy) as src,sqlite3.connect(direct/'gateway.sqlite3') as dst:src.backup(dst)
    upgraded=Database(Settings(direct,g['settings'].key_file,min_free_bytes=0))
    upgraded.initialize();upgraded.initialize()
    with upgraded.connect() as c:
        assert c.execute('SELECT version FROM schema_version').fetchone()[0]==6
        for table,rows in before.items():
            assert [tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY id')]==rows
        attempts=c.execute('SELECT * FROM upstream_attempts').fetchall()
        assert len(attempts)==2 and all(r['status']=='history' and r['revision'] is None and r['dispatched_at'] is None for r in attempts)
    result=verify(snapshot,tmp_path/'guarded-restore')
    assert result['ledger_before']['ok'] and result['ledger_after']['ok']
    restored=Database(Settings.load(tmp_path/'guarded-restore/config/settings.json'))
    assert restored.one('SELECT version FROM schema_version')['version']==6
    assert restored.one("SELECT count(*) n FROM requests WHERE status='running'")['n']==0
    assert restored.one("SELECT count(*) n FROM api_keys WHERE active=1")['n']==0
    assert sqlite3.connect(legacy).execute('SELECT version FROM schema_version').fetchone()[0]==1


def test_schema_migration_rolls_back_all_ddl_on_failure(tmp_path):
    from aigateway.migrations import migrate
    c=sqlite3.connect(tmp_path/'legacy.sqlite3',isolation_level=None)
    c.executescript(SCHEMA)
    c.execute('CREATE TABLE upstream_probes(existing TEXT)')
    with pytest.raises(sqlite3.OperationalError):migrate(c)
    assert c.execute('SELECT version FROM schema_version').fetchone()[0]==1
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('upstream_runtime','upstream_attempts')").fetchall()
    c.close()


@pytest.mark.parametrize('streaming',[False,True])
def test_one_deadline_covers_headers_and_body(gateway,streaming):
    g=configured(gateway)
    g['client'].put('/api/v1/admin/policies/key/'+g['key_id'],json={'timeout':1})
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.7)
            if streaming:
                yield b'data: [DONE]\n\n'
            else:
                yield json.dumps(chat_result()).encode()
    async def slow(request):
        await asyncio.sleep(.5)
        return httpx.Response(200,stream=SlowBody(),headers={'content-type':'text/event-stream' if streaming else 'application/json'})
    g['app'].state.upstream_transport=httpx.MockTransport(slow)
    start=time.monotonic()
    r=g['client'].post('/v1/chat/completions',json={**BODY,'stream':streaming},headers=g['headers'])
    assert time.monotonic()-start<1.7
    assert r.status_code==(200 if streaming else 504)
    assert g['db'].one('SELECT status,charged_micro FROM requests')=={'status':'unknown','charged_micro':0}
    assert ledger_check(g['db'])['ok']


def test_real_tcp_pool_reuses_socket_without_cross_credential_cookies(gateway):
    g=configured(gateway);seen=[]
    vendor=FastAPI()
    @vendor.post('/v1/chat/completions')
    async def echo(request:Request):
        seen.append({'port':request.client.port,'auth':request.headers.get('authorization'),'cookie':request.headers.get('cookie')})
        response=JSONResponse(chat_result());response.set_cookie('provider-session','must-not-cross-credentials')
        return response
    with serve(vendor) as url:
        g['app'].state.upstream_transport=None
        update={'name':'真实TCP上游A','pool_id':g['pool'],'provider':'compatible','base_url':url+'/v1',
            'api_key':'synthetic-first-ONLY-QA','enabled':True,'concurrency':10}
        assert g['client'].put('/api/v1/admin/upstreams/'+g['upstream'],json=update).status_code==200
        assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==200
        update.update(name='真实TCP上游B',api_key='synthetic-second-ONLY-QA')
        assert g['client'].post('/api/v1/admin/upstreams',json=update).status_code==201
        assert g['client'].post('/v1/chat/completions',json=BODY,headers=g['headers']).status_code==200
    assert len(seen)==2 and seen[0]['port']==seen[1]['port']
    assert seen[0]['auth']=='Bearer synthetic-first-ONLY-QA' and seen[1]['auth']=='Bearer synthetic-second-ONLY-QA'
    assert not seen[0]['cookie'] and not seen[1]['cookie']

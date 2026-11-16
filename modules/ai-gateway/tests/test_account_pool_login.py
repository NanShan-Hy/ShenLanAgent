"""Remote loopback callbacks, safe diagnostics, and retired integration migration."""
import asyncio
from contextlib import closing
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from aigateway.account_pool_api import PREFIX
from aigateway.core import Database, Settings
from aigateway.billing import ledger_check
from test_account_pool import pool,begin
from conftest import configured

CALLBACK='/auth/callback?state=QA-STATE&code=QA-code'


def test_remote_callback_does_not_require_browser_portal_cookie_and_cleans_url(pool):
    g=pool;flow=begin(g)
    # This represents localhost:1455 forwarded to the gateway, a different host
    # from the portal's 127.0.0.1:8094 and thus without its Strict cookie.
    from fastapi.testclient import TestClient
    with closing(TestClient(g['app'])) as browser:
        r=browser.get(CALLBACK,follow_redirects=False)
        assert r.status_code==303 and 'QA-code' not in r.headers['location']
        assert r.headers['referrer-policy']=='no-referrer' and r.headers['cache-control']=='no-store'
        assert not browser.cookies
        assert browser.get(r.headers['location']).status_code==200
    assert g['control']['exchanges']==1
    g['control']['status']='ok'
    data=g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()
    assert data['status']=='ok' and data['saved'] and data['adopted'] and data['account_id']
    audit=g['db'].all("SELECT detail FROM audit WHERE action LIKE 'account_pool.authorization_%'")
    assert audit and all('QA-code' not in x['detail'] and 'QA-STATE' not in x['detail'] for x in audit)


def test_auto_and_manual_concurrent_callback_exchange_exactly_once(pool):
    g=pool;flow=begin(g)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=g['app']),base_url='http://testserver',
            cookies=dict(g['client'].cookies),headers={'X-Requested-With':'AIGateway'}) as c:
            responses=await asyncio.gather(c.get(CALLBACK),c.post(PREFIX+'/authorizations/'+flow['id']+'/callback',
                json={'redirect_url':'http://localhost:1455'+CALLBACK}))
            assert responses[0].status_code==303
            assert responses[1].status_code in (200,409)
    asyncio.run(run())
    assert g['control']['exchanges']==1


@pytest.mark.parametrize('action',['logout','expire','disable'])
def test_auto_callback_rejects_ended_or_expired_session(pool,action):
    g=pool;flow=begin(g)
    if action=='logout':g['client'].post('/api/v1/auth/logout',json={})
    elif action=='expire':g['app'].state.account_pool_flows[flow['id']]['expires_at']=0
    else:g['db'].execute('UPDATE users SET active=0 WHERE id=?',(g['user'],))
    assert g['client'].get(CALLBACK,follow_redirects=False).status_code==303
    assert g['control']['exchanges']==0


def test_saved_is_not_success_until_adopted_and_account_present(pool):
    g=pool;flow=begin(g);g['control'].update(status='ok',adopted=False)
    result=g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()
    assert result['status']=='wait' and result['phase']=='loading' and result['saved']
    g['control']['adoption_error']='PRIVATE_BACKEND_DETAIL'
    r=g['client'].get(PREFIX+'/authorizations/'+flow['id'])
    assert r.json()['error_code']=='account_pool_credential_load_failed' and 'PRIVATE_BACKEND_DETAIL' not in r.text
    g['control'].update(adopted=True,files=[])
    assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()['status']=='wait'
    g['control']['files']=[{'name':'codex-qa.json','provider':'codex','status':'active'}]
    assert g['client'].get(PREFIX+'/authorizations/'+flow['id']).json()['status']=='ok'


@pytest.mark.parametrize('raw,suffix,http_status',[
    ('unknown or expired state','authorization_expired',None),
    ('State code error','callback_state_mismatch',None),
    ('Failed to exchange authorization code for tokens: invalid_grant','authorization_code_rejected',None),
    ('Failed to exchange authorization code for tokens: token exchange failed with status 403: SENSITIVE','token_exchange_rejected',403),
    ('Failed to exchange authorization code for tokens: status 403: unsupported_country_region_territory','region_unsupported',403),
    ('Failed to exchange authorization code for tokens: dial tcp: i/o timeout','authorization_network_failed',None),
    ('Failed to save authentication tokens','credential_save_failed',None),
    ('unclassified SENSITIVE','authorization_failed',None),
])
def test_oauth_error_categories_never_expose_backend_secrets(pool,raw,suffix,http_status):
    g=pool;flow=begin(g);g['control'].update(status='error',oauth_error=raw+' ACCESS_TOKEN_PRIVATE')
    r=g['client'].get(PREFIX+'/authorizations/'+flow['id'])
    assert r.json()['error_code']=='account_pool_'+suffix
    assert r.json()['http_status']==http_status
    assert 'SENSITIVE' not in r.text and 'ACCESS_TOKEN_PRIVATE' not in r.text
    assert 'ACCESS_TOKEN_PRIVATE' not in json.dumps(g['db'].all('SELECT detail FROM audit'))


def test_old_endpoints_are_gone_and_schema3_resources_retire_without_financial_changes(gateway,tmp_path):
    g=configured(gateway)
    for route in ['/api/v1/chatgpt/status','/api/v1/chatgpt/accounts','/api/v1/admin/chatgpt/integration','/auth/chatgpt/callback']:
        assert g['client'].get(route).status_code==404
    destination=tmp_path/'legacy-data';destination.mkdir()
    with g['db'].connect() as source,sqlite3.connect(destination/'gateway.sqlite3') as target:source.backup(target)
    with sqlite3.connect(destination/'gateway.sqlite3') as c:
        from conftest import drop_v6
        drop_v6(c)
        for table in ('request_capture_chunks','request_audits','request_recording_settings'):
            c.execute('DROP TABLE '+table)
        for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_frozen_%'").fetchall():
            c.execute('DROP TRIGGER '+name)
        c.execute('UPDATE schema_version SET version=3')
        c.execute("UPDATE pools SET kind='chatgpt' WHERE id=?",(g['pool'],))
        original={t:c.execute('SELECT * FROM '+t).fetchall() for t in ('ledger','orders','requests','api_keys')}
    db=Database(Settings(destination,g['settings'].key_file,min_free_bytes=0));db.initialize();db.initialize()
    assert db.one('SELECT version FROM schema_version')['version']==6
    assert db.one('SELECT enabled,approval_status FROM pools')=={'enabled':0,'approval_status':'retired'}
    assert db.one('SELECT enabled FROM upstreams')['enabled']==0
    assert db.one('SELECT enabled FROM models')['enabled']==0
    with db.connect() as c:
        for t,rows in original.items():assert [tuple(r) for r in c.execute('SELECT * FROM '+t)]==rows
        with pytest.raises(sqlite3.IntegrityError):c.execute('UPDATE chatgpt_integration SET enabled=1')
    assert ledger_check(db)['ok']


def test_result_page_is_not_a_manual_callback_and_never_claims_final_login_success(pool):
    g=pool;flow=begin(g)
    url='http://localhost:1455/auth/callback/result?status=accepted'
    r=g['client'].post(PREFIX+'/authorizations/'+flow['id']+'/callback',json={'redirect_url':url})
    assert r.status_code==400 and r.json()['error']['code']=='account_pool_callback_result_page'
    assert g['control']['exchanges']==0
    accepted=g['client'].get('/auth/callback/result?status=accepted')
    assert '登录回调已接收' in accepted.text and '无需复制地址或再次手动提交' in accepted.text
    assert '不代表账号登录已经成功' in accepted.text
    unknown=g['client'].get('/auth/callback/result?status=check_console')
    assert '未确认本次回调已接收' in unknown.text

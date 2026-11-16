import json

import httpx
import pytest
from fastapi.testclient import TestClient

from aigateway.billing import ledger_check
from aigateway.request_audit import AuditContext
from conftest import configured, chat_result, HEADERS, PASSWORD
from test_clients import response_stream

BASE = '/api/v1/admin/request-audits'


def transcript(g, trace, section):
    cursor, text = 0, ''
    while cursor is not None:
        r = g['client'].get(BASE+'/'+trace+'/content',params={'section':section,'cursor':cursor})
        assert r.status_code == 200, r.text
        text += r.json()['text'];cursor = r.json()['next_cursor']
    return text


def post(g, message='CURRENT_PRIVATE_QUESTION', **extra):
    return g['client'].post('/v1/chat/completions',json={'model':'qa-model',
        'messages':[{'role':'user','content':message}], 'max_tokens':64,**extra},headers=g['headers'])


def test_current_round_encrypted_redacted_and_original_forwarding_unchanged(gateway):
    g=configured(gateway);sent=[]
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:sent.append(json.loads(r.content)) or httpx.Response(200,json=chat_result()))
    messages=[{'role':'user','content':'OLD_HISTORY_DO_NOT_SAVE'}, {'role':'assistant','content':'old answer'},
              {'role':'system','content':'PRIVATE_SYSTEM_DO_NOT_SAVE'},
              {'role':'user','content':'CURRENT_PRIVATE_QUESTION '+g['token']},
              {'role':'tool','tool_call_id':'call-one','content':'{"password":"SYNTHETIC_PASSWORD_VALUE"}'}]
    r=g['client'].post('/v1/chat/completions',json={'model':'qa-model','messages':messages,'max_tokens':64},headers=g['headers'])
    assert r.status_code==200
    trace=r.headers['x-gateway-trace-id']
    assert r.headers['x-request-id'] != trace
    record=g['client'].get(BASE+'/'+trace).json()
    assert record['display_name']=='运营管理员' and record['status']=='succeeded'
    assert record['capture_status']=='complete' and record['first_byte_ms'] is not None
    assert sent[0]['messages']==messages
    text=transcript(g,trace,'input');tools=transcript(g,trace,'tools')
    assert 'CURRENT_PRIVATE_QUESTION' in text and '[REDACTED]' in text
    assert g['token'] not in text and 'OLD_HISTORY_DO_NOT_SAVE' not in text and 'PRIVATE_SYSTEM_DO_NOT_SAVE' not in text
    assert 'SYNTHETIC_PASSWORD_VALUE' not in tools and 'client_return' in tools
    assert '本地协议回答' in transcript(g,trace,'output')
    raw=g['db'].path.read_bytes()
    for private in (b'CURRENT_PRIVATE_QUESTION',b'SYNTHETIC_PASSWORD_VALUE',g['token'].encode()):assert private not in raw
    assert g['db'].one("SELECT count(*) n FROM audit WHERE action='request_contents.view'")['n']>=3
    assert ledger_check(g['db'])['ok']


def test_rejected_requests_have_trace_and_body_but_no_financial_charge(gateway):
    g=configured(gateway)
    g['client'].put('/api/v1/admin/policies/user/'+g['user'],json={'tpm':0})
    r=post(g);assert r.status_code==429
    trace=r.headers['x-gateway-trace-id'];record=g['client'].get(BASE+'/'+trace).json()
    assert record['status']=='rejected' and record['error_code']=='quota_exceeded'
    assert record['user_id']==g['user'] and record['request_id'] is None and record['charged_micro']==0
    assert 'CURRENT_PRIVATE_QUESTION' in transcript(g,trace,'input')
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==0
    bad=g['client'].post('/v1/responses',headers={'Authorization':'Bearer sk-gw-INVALID-KEY'},json={'model':'qa-model','input':'ANONYMOUS_BODY_NEVER_SAVE'})
    assert bad.status_code==401
    record=g['client'].get(BASE+'/'+bad.headers['x-gateway-trace-id']).json()
    assert record['user_id'] is None and record['capture_bytes']==0
    assert b'ANONYMOUS_BODY_NEVER_SAVE' not in g['db'].path.read_bytes()


def test_idempotent_attempts_are_separate_without_duplicate_cost(gateway):
    g=configured(gateway)
    headers={**g['headers'],'Idempotency-Key':'audit-once'}
    body={'model':'qa-model','messages':[{'role':'user','content':'Only once'}],'max_tokens':64}
    a=g['client'].post('/v1/chat/completions',json=body,headers=headers)
    b=g['client'].post('/v1/chat/completions',json=body,headers=headers)
    assert (a.status_code,b.status_code)==(200,409)
    assert a.headers['x-request-id']==b.headers['x-request-id']
    assert a.headers['x-gateway-trace-id']!=b.headers['x-gateway-trace-id']
    rows=g['client'].get(BASE).json()
    assert rows['total']==2 and rows['summary']['charged_micro']==g['db'].one('SELECT charged_micro FROM requests')['charged_micro']
    rejected=next(x for x in rows['items'] if not x['execution_owner'])
    assert rejected['charged_micro']==0 and rejected['request_id']==a.headers['x-request-id']


def test_only_global_admin_can_read_capture_or_settings(gateway):
    g=configured(gateway);r=post(g);trace=r.headers['x-gateway-trace-id']
    created=g['client'].post('/api/v1/admin/users',json={'username':'audit-other','display_name':'普通用户','password':PASSWORD}).json()
    c=TestClient(g['app']);c.headers.update(HEADERS)
    assert c.post('/api/v1/auth/login',json={'username':'audit-other','password':PASSWORD}).status_code==200
    for url in (BASE,BASE+'/options',BASE+'/'+trace,BASE+'/'+trace+'/content','/api/v1/admin/request-audit-settings'):
        response=c.get(url);assert response.status_code==403 and 'CURRENT_PRIVATE_QUESTION' not in response.text
    assert c.post(BASE+'/cleanup/preview',json={'ids':[trace]}).status_code==403
    assert c.patch('/api/v1/admin/request-audit-settings',json={'enabled':False}).status_code==403
    assert 'CURRENT_PRIVATE_QUESTION' not in c.get('/api/v1/usage').text


def test_storage_limit_fails_open_and_manual_cleanup_preserves_ledger(gateway):
    g=configured(gateway);c=g['client']
    assert c.patch('/api/v1/admin/request-audit-settings',json={'max_bytes':1048576}).status_code==200
    g['db'].execute('UPDATE models SET context_limit=2000000')
    r=post(g,'CAPACITY_PRIVATE_'+'x'*900000);assert r.status_code==200
    trace=r.headers['x-gateway-trace-id'];record=c.get(BASE+'/'+trace).json()
    assert record['status']=='succeeded' and record['capture_status']=='partial' and record['capture_reason']=='storage_limit'
    settings=c.get('/api/v1/admin/request-audit-settings').json()
    assert 0<settings['used_bytes']<=settings['max_bytes']
    before=g['db'].all('SELECT * FROM ledger ORDER BY id')
    assert c.post(BASE+'/cleanup',json={'ids':[trace]}).status_code==409
    preview=c.post(BASE+'/cleanup/preview',json={'ids':[trace]}).json();assert preview['count']==1
    result=c.post(BASE+'/cleanup',json={'ids':preview['ids'],'selection_hash':preview['selection_hash']})
    assert result.status_code==200
    assert c.get('/api/v1/admin/request-audit-settings').json()['used_bytes']==0
    assert c.get(BASE+'/'+trace+'/content').json()['capture_status']=='cleared'
    assert g['db'].all('SELECT * FROM ledger ORDER BY id')==before
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==1 and ledger_check(g['db'])['ok']


def test_capture_failure_does_not_fail_model_call(gateway,monkeypatch):
    g=configured(gateway)
    def fail_write(*a,**k):raise OSError('synthetic unavailable writer')
    monkeypatch.setattr(AuditContext,'_write',fail_write)
    r=post(g);assert r.status_code==200
    record=g['client'].get(BASE+'/'+r.headers['x-gateway-trace-id']).json()
    assert record['status']=='succeeded' and record['capture_status']=='partial'
    assert ledger_check(g['db'])['ok']


def test_disabled_recording_and_corrupt_content(gateway):
    g=configured(gateway);c=g['client']
    c.patch('/api/v1/admin/request-audit-settings',json={'enabled':False})
    r=post(g,'DISABLED_PRIVATE');record=c.get(BASE+'/'+r.headers['x-gateway-trace-id']).json()
    assert record['capture_status']=='disabled' and record['capture_bytes']==0
    c.patch('/api/v1/admin/request-audit-settings',json={'enabled':True})
    r=post(g);trace=r.headers['x-gateway-trace-id']
    g['db'].execute("UPDATE request_capture_chunks SET ciphertext='corrupted' WHERE audit_id=?",(trace,))
    assert c.get(BASE+'/'+trace+'/content').status_code==409


def test_multi_byte_content_pagination_and_filters(gateway):
    g=configured(gateway)
    g['db'].execute('UPDATE models SET context_limit=2000000')
    message='分页中文🧪'*30000
    r=post(g,message);assert r.status_code==200
    trace=r.headers['x-gateway-trace-id'];saved=json.loads(transcript(g,trace,'input'))
    assert saved[0]['content']==message
    assert g['client'].get(BASE,params={'user_id':g['user'],'application_id':g['application'],'trace_id':trace}).json()['total']==1
    assert g['client'].get(BASE,params={'user_id':'not-real'}).json()['total']==0
    assert g['client'].get(BASE,params={'from_time':100,'to_time':1}).status_code==400


def test_sse_merged_frames_are_valid_and_saved_once(gateway):
    g=configured(gateway);text='x'*(1100*1024);data=response_stream(text).content
    assert max(map(len,data.split(b'\n\n')))<2*1024**2 and len(data)>2*1024**2
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,content=data,headers={'content-type':'text/event-stream'}))
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'Synthetic SSE','stream':True},headers=g['headers'])
    assert r.status_code==200 and 'stream_interrupted' not in r.text
    assert g['db'].one('SELECT status FROM requests')['status']=='succeeded'
    assert transcript(g,r.headers['x-gateway-trace-id'],'output')==text


def test_pre_send_invalid_credential_releases_hold_and_is_audited(gateway):
    g=configured(gateway);sent=[]
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:sent.append(r) or httpx.Response(200,json=chat_result()))
    g['db'].execute("UPDATE upstreams SET secret='invalid-synthetic-ciphertext'")
    r=post(g)
    assert r.status_code==502 and r.json()['error']['code']=='upstream_credential_unavailable'
    assert not sent and g['db'].one('SELECT status FROM requests')['status']=='failed'
    assert g['db'].one('SELECT sum(reserved) n FROM buckets')['n']==0
    record=g['client'].get(BASE+'/'+r.headers['x-gateway-trace-id']).json()
    assert record['error_code']=='upstream_credential_unavailable' and record['stage']=='credentials'
    assert ledger_check(g['db'])['ok']


def test_capture_backup_restore_and_schema4_upgrade_preserve_finance(gateway,tmp_path):
    import sqlite3
    from pathlib import Path
    from aigateway.core import Settings,Database
    from aigateway.cli import backup,verify
    g=configured(gateway);r=post(g,'BACKUP_CAPTURE_PRIVATE');trace=r.headers['x-gateway-trace-id']
    snapshot=backup(g['db'],source=Path(__file__).resolve().parents[1],root=tmp_path/'backups')
    result=verify(snapshot,tmp_path/'restore')
    assert result['capture_chunks_decrypted']>=2 and result['ledger_after']['ok']
    restored=Database(Settings.load(tmp_path/'restore/config/settings.json'))
    row=restored.one("SELECT ciphertext FROM request_capture_chunks WHERE audit_id=? AND section='input'",(trace,))
    assert 'BACKUP_CAPTURE_PRIVATE' in restored.settings.unseal(row['ciphertext'])['text']
    assert b'BACKUP_CAPTURE_PRIVATE' not in (snapshot/'data/gateway.sqlite3').read_bytes()
    old=tmp_path/'schema4';old.mkdir()
    with g['db'].connect() as source,sqlite3.connect(old/'gateway.sqlite3') as c:
        source.backup(c)
        from conftest import drop_v6
        drop_v6(c)
        for table in ('request_capture_chunks','request_audits','request_recording_settings'):c.execute('DROP TABLE '+table)
        c.execute('UPDATE schema_version SET version=4')
        before={t:c.execute('SELECT * FROM '+t).fetchall() for t in ('requests','ledger','buckets','api_keys','models')}
    migrated=Database(Settings(old,g['settings'].key_file,min_free_bytes=0));migrated.initialize();migrated.initialize()
    with migrated.connect() as c:
        for t,rows in before.items():assert [tuple(r) for r in c.execute('SELECT * FROM '+t)]==rows
    assert migrated.one('SELECT capture_status FROM request_audits')['capture_status']=='historical'
    assert migrated.one('SELECT used_bytes FROM request_recording_settings')['used_bytes']==0


def test_sse_secret_across_chunks_and_tool_call_arguments(gateway):
    g=configured(gateway)
    text='a'*65000+'\n{"password":"SPLIT_PASSWORD_PRIVATE"}\n'+g['token']+'\n'+'z'*9000
    data=response_stream(text).content
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,content=data,headers={'content-type':'text/event-stream'}))
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'Synthetic split test','stream':True},headers=g['headers'])
    assert r.status_code==200
    saved=transcript(g,r.headers['x-gateway-trace-id'],'output')
    assert 'SPLIT_PASSWORD_PRIVATE' not in saved and g['token'] not in saved and '[REDACTED]' in saved
    item={'type':'function_call','id':'fc-audit','call_id':'call-audit','name':'exec_command',
          'arguments':json.dumps({'cmd':'echo local','api_key':'TOOL_ARGUMENT_SECRET','token':'TOOL_TOKEN_SECRET','secret_key':'TOOL_OTHER_SECRET'})}
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:response_stream('',item))
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'Synthetic tools','stream':True},headers=g['headers'])
    saved=transcript(g,r.headers['x-gateway-trace-id'],'tools')
    assert 'exec_command' in saved and 'echo local' in saved and 'TOOL_ARGUMENT_SECRET' not in saved and 'model_request' in saved
    assert 'TOOL_TOKEN_SECRET' not in saved and 'TOOL_OTHER_SECRET' not in saved


def test_stream_disconnect_preserves_partial_text_without_false_completion(gateway):
    g=configured(gateway)
    data=b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"PARTIAL_VISIBLE_TEXT"}\n\n'
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,content=data,headers={'content-type':'text/event-stream'}))
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'Interrupted synthetic stream','stream':True},headers=g['headers'])
    trace=r.headers['x-gateway-trace-id'];record=g['client'].get(BASE+'/'+trace).json()
    assert record['status']=='unknown' and record['capture_status']=='partial'
    assert transcript(g,trace,'output')=='PARTIAL_VISIBLE_TEXT'
    assert g['db'].one('SELECT charged_micro FROM requests')['charged_micro']==0


def test_genuinely_oversized_sse_event_still_rejected(gateway):
    g=configured(gateway);data=response_stream('z'*(2*1024**2+1)).content
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,content=data,headers={'content-type':'text/event-stream'}))
    r=g['client'].post('/v1/responses',json={'model':'qa-model','input':'Over event limit','stream':True},headers=g['headers'])
    assert 'stream_interrupted' in r.text
    assert g['db'].one('SELECT status,charged_micro FROM requests')=={'status':'unknown','charged_micro':0}
    assert ledger_check(g['db'])['ok']


def test_compact_records_readable_scope_without_opaque_provider_state(gateway):
    g=configured(gateway)
    raw={'id':'compact-audit','object':'response.compaction','output':[{'type':'compaction','encrypted_content':'PROVIDER_OPAQUE_STATE_NEVER_CAPTURE'}],
         'usage':{'input_tokens':80,'output_tokens':10}}
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw))
    r=g['client'].post('/v1/responses/compact',json={'model':'qa-model','input':'Compact this current turn'},headers=g['headers'])
    assert r.status_code==200
    saved=transcript(g,r.headers['x-gateway-trace-id'],'output')
    assert '加密压缩状态' in saved and 'PROVIDER_OPAQUE' not in saved


def test_concurrent_capture_capacity_accounting_is_atomic(gateway):
    from concurrent.futures import ThreadPoolExecutor
    g=configured(gateway)
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses=list(pool.map(lambda n:post(g,'Concurrent 内容 '+str(n)+'x'*20000),range(5)))
    assert all(r.status_code==200 for r in responses)
    settings=g['client'].get('/api/v1/admin/request-audit-settings').json()
    actual=g['db'].one('SELECT COALESCE(sum(byte_size),0) n FROM request_capture_chunks')['n']
    declared=g['db'].one('SELECT COALESCE(sum(capture_bytes),0) n FROM request_audits')['n']
    assert 0<actual==declared==settings['used_bytes']<=settings['max_bytes']
    assert ledger_check(g['db'])['ok']


def test_trace_financial_binding_survives_crash_and_recovery(gateway):
    from aigateway.routing import admit
    from aigateway.billing import maintenance
    from aigateway.request_audit import recover
    g=configured(gateway)
    context=AuditContext(g['db'],{'path':'/v1/chat/completions','headers':[(b'authorization',('Bearer '+g['token']).encode())], 'client':('testclient',0)})
    held=admit(g['db'],g['token'],'testclient',{'model':'qa-model','messages':[{'role':'user','content':'Crash binding fixture'}]},'chat','/chat/completions',audit_id=context.id)
    row=g['db'].one('SELECT request_id,execution_owner FROM request_audits WHERE id=?',(context.id,))
    assert row=={'request_id':held['id'],'execution_owner':1}
    maintenance(g['db'],restart=True);recover(g['db'])
    row=g['db'].one('SELECT status,capture_status FROM request_audits WHERE id=?',(context.id,))
    assert row=={'status':'unknown','capture_status':'partial'}
    assert ledger_check(g['db'])['ok']

import io
import json
import time

import httpx
from PIL import Image

from aigateway import managed_store
from aigateway.billing import ledger_check
from aigateway.conversations import recover
from conftest import configured,chat_result


def conversation(g,**extra):
    r=g['client'].post('/api/v1/conversations/resolve',headers=g['headers'],json={'model':'qa-model','protocol':'chat',**extra})
    assert r.status_code==200,r.text
    return r.json()


def send(g,c,text='new question',**extra):
    headers={**g['headers'],'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':c['id'],
             'X-Gateway-Context-Revision':str(c['revision']),'Idempotency-Key':'turn-'+str(time.time_ns())}
    return g['client'].post('/v1/chat/completions',headers=headers,json={'model':'qa-model','messages':[{'role':'user','content':text}],'max_tokens':64,**extra})


def test_web_chat_resumes_by_type_replays_history_and_is_encrypted(gateway):
    g=configured(gateway);seen=[]
    g['app'].state.upstream_transport=httpx.MockTransport(lambda r:seen.append(json.loads(r.content)) or httpx.Response(200,json=chat_result()))
    c=conversation(g,task_type='sales');assert conversation(g,task_type='sales')['id']==c['id']
    assert conversation(g,task_type='other')['id']!=c['id']
    r=send(g,c,'MANAGED_PRIVATE_FIRST');assert r.status_code==200,r.text
    assert r.headers['x-gateway-context-revision']=='1'
    c=conversation(g,task_type='sales');assert c['revision']==1
    r=send(g,c,'MANAGED_PRIVATE_SECOND');assert r.status_code==200,r.text
    assert [x['role'] for x in seen[-1]['messages']]==['user','assistant','user']
    assert seen[-1]['messages'][0]['content']=='MANAGED_PRIVATE_FIRST'
    assert b'MANAGED_PRIVATE_FIRST' not in g['db'].path.read_bytes()
    assert ledger_check(g['db'])['ok'];assert g['db'].one('SELECT reserved_bytes FROM managed_settings')['reserved_bytes']==0
    assert g['client'].get('/api/v1/conversations/'+c['id'],headers=g['headers']).json()['revision']==2


def test_subject_isolation_revision_and_explicit_new_session(gateway):
    g=configured(gateway);client=g['client']
    assert client.put('/api/v1/admin/conversation-policies',json={'application_id':g['application'],'policy':'strict','identity_mode':'application_subject'}).status_code==200
    r=client.post('/api/v1/conversations/resolve',headers=g['headers'],json={'model':'qa-model'});assert r.status_code==400
    a=client.post('/api/v1/conversations/resolve',headers={**g['headers'],'X-Gateway-Subject':'A'},json={'model':'qa-model','protocol':'chat'}).json()
    b=client.post('/api/v1/conversations/resolve',headers={**g['headers'],'X-Gateway-Subject':'B'},json={'model':'qa-model','protocol':'chat'}).json()
    assert a['id']!=b['id'] and a['policy']=='strict'
    assert client.get('/api/v1/conversations/'+a['id'],headers={**g['headers'],'X-Gateway-Subject':'B'}).status_code==404
    g={**g,'headers':{**g['headers'],'X-Gateway-Subject':'A'}}
    assert send(g,a).status_code==200
    assert send(g,a).status_code==409
    assert conversation(g,new_session=True)['id']!=a['id']


def test_tool_request_and_external_return_roundtrip(gateway):
    g=configured(gateway);seen=[]
    def provider(request):
        body=json.loads(request.content);seen.append(body);raw=chat_result()
        if len(seen)==1:raw['choices'][0]['message']={'role':'assistant','content':None,'tool_calls':[{'id':'mcp-call-one','type':'function','function':{'name':'mcp_sales_search','arguments':'{"query":"sales"}'}}]}
        return httpx.Response(200,json=raw)
    g['app'].state.upstream_transport=httpx.MockTransport(provider)
    c=conversation(g);assert send(g,c,tools=[{'type':'function','function':{'name':'mcp_sales_search','parameters':{'type':'object'}}}]).status_code==200
    c=conversation(g);assert c['state']=='waiting_tools'
    assert send(g,c,'do another task').status_code==409
    r=send(g,c,messages=[{'role':'tool','tool_call_id':'mcp-call-one','content':'CLIENT_MCP_RESULT'}]);assert r.status_code==200,r.text
    assert 'CLIENT_MCP_RESULT' in json.dumps(seen[-1])
    assert len(seen)==2 and conversation(g)['pending_calls']==[]


def test_full_storage_rejects_before_inference_and_cleanup_preserves_finance(gateway):
    g=configured(gateway);c=conversation(g)
    assert send(g,c).status_code==200;c=conversation(g)
    assert g['client'].patch('/api/v1/admin/conversation-settings',json={'max_bytes':1048576}).status_code==200
    before=g['db'].one('SELECT count(*) n FROM requests')['n'];r=send(g,c);assert r.status_code==507
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==before
    preview=g['client'].post('/api/v1/admin/conversations/'+c['id']+'/cleanup/preview').json()
    assert preview['objects']>=2
    r=g['client'].post('/api/v1/admin/conversations/'+c['id']+'/cleanup',json={'selection_hash':preview['selection_hash']})
    assert r.status_code==200 and ledger_check(g['db'])['ok']
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==before
    assert g['db'].one('SELECT used_bytes FROM managed_settings')['used_bytes']==0


def test_private_file_upload_text_context_and_delete(gateway):
    g=configured(gateway);client=g['client']
    uploaded=client.post('/v1/files',headers=g['headers'],data={'purpose':'user_data'},files={'file':('report.txt',b'FILE_PRIVATE_ANALYSIS','text/plain')})
    assert uploaded.status_code==201,uploaded.text
    pk=uploaded.json()['id'];assert b'FILE_PRIVATE_ANALYSIS' not in g['db'].path.read_bytes()
    seen=[];g['app'].state.upstream_transport=httpx.MockTransport(lambda r:seen.append(json.loads(r.content)) or httpx.Response(200,json=chat_result()))
    c=conversation(g);r=send(g,c,messages=[{'role':'user','content':[{'type':'text','text':'analyze'},{'type':'file','file':{'file_id':pk}}]}])
    assert r.status_code==200,r.text;assert 'FILE_PRIVATE_ANALYSIS' in json.dumps(seen[-1])
    assert client.get('/v1/files/'+pk+'/content',headers=g['headers']).content==b'FILE_PRIVATE_ANALYSIS'
    assert client.delete('/v1/files/'+pk,headers=g['headers']).status_code==200
    assert client.get('/v1/files/'+pk+'/content',headers=g['headers']).status_code==404


def test_images_require_verified_capability_and_budget_excludes_base64(gateway):
    g=configured(gateway);out=io.BytesIO();Image.new('RGB',(1024,1024),'red').save(out,format='PNG')
    file=g['client'].post('/v1/files',headers=g['headers'],files={'file':('photo.png',out.getvalue(),'image/png')}).json()['id']
    content=[{'type':'text','text':'look'},{'type':'file','file':{'file_id':file}}]
    c=conversation(g);assert send(g,c,messages=[{'role':'user','content':content}]).status_code==400
    caps=g['client'].put('/api/v1/admin/model-media/'+g['model'],json={'vision':True,'verified':True,'image_tokens':2048,'max_images':4});assert caps.status_code==200
    c=conversation(g);r=send(g,c,messages=[{'role':'user','content':content}]);assert r.status_code==200,r.text
    req=g['db'].one('SELECT estimate_tokens FROM requests ORDER BY created_at DESC LIMIT 1')
    assert 2048<=req['estimate_tokens']<10000
    assert ledger_check(g['db'])['ok']


def test_restart_preserves_context_and_marks_unfinished_turn(gateway):
    g=configured(gateway);c=conversation(g);assert send(g,c).status_code==200
    assert managed_store.verify(g['db'])>=2
    g['db'].execute('UPDATE conversations SET lease=?,lease_until=? WHERE id=?',('interrupted',time.time()+60,c['id']))
    recover(g['db']);assert conversation(g)['state']=='needs_recovery'
    r=g['client'].post('/api/v1/conversations/'+c['id']+'/recover',headers=g['headers'],json={'acknowledge_unknown':True})
    assert r.status_code==200 and r.json()['replayed'] is False
    assert send(g,conversation(g)).status_code==200


def test_long_history_summary_is_billed_and_originals_remain(gateway):
    g=configured(gateway);g['db'].execute('UPDATE models SET context_limit=10000,max_output=128')
    seen=[]
    def provider(request):
        body=json.loads(request.content);seen.append(body);raw=chat_result()
        if '仅总结提供的历史资料' in json.dumps(body,ensure_ascii=False):raw['choices'][0]['message']['content']='早期目标：持续分析销售；保留来源。'
        return httpx.Response(200,json=raw)
    g['app'].state.upstream_transport=httpx.MockTransport(provider)
    for i in range(12):
        c=conversation(g);r=send(g,c,('HISTORY_ORIGINAL_'+str(i)+' ')*45)
        assert r.status_code==200,r.text
    row=g['db'].one('SELECT * FROM conversations WHERE id=?',(c['id'],));assert row['summary_id']
    summaries=g['db'].all("SELECT * FROM conversation_turns WHERE kind='summary'")
    assert summaries and all(r['status']=='succeeded' and r['request_id'] for r in summaries)
    assert g['db'].one("SELECT count(*) n FROM conversation_turns WHERE kind='answer' AND status='succeeded'")['n']==12
    first=g['db'].one("SELECT input_id FROM conversation_turns WHERE kind='answer' ORDER BY created_at LIMIT 1")
    assert 'HISTORY_ORIGINAL_0' in json.dumps(managed_store.get(g['db'],first['input_id']))
    assert ledger_check(g['db'])['ok'] and g['db'].one('SELECT reserved_bytes FROM managed_settings')['reserved_bytes']==0


def test_responses_stream_commits_context_before_terminal(gateway):
    from test_clients import response_stream
    g=configured(gateway);g['app'].state.upstream_transport=httpx.MockTransport(lambda r:response_stream('MANAGED_STREAM_OUTPUT'))
    c=conversation(g,protocol='responses')
    headers={**g['headers'],'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':c['id'],'X-Gateway-Context-Revision':'0','Idempotency-Key':'stream-once'}
    r=g['client'].post('/v1/responses',headers=headers,json={'model':'qa-model','input':'stream question','stream':True,'max_output_tokens':64})
    assert r.status_code==200 and 'response.completed' in r.text
    row=g['db'].one("SELECT * FROM conversation_turns WHERE kind='answer'");assert row['status']=='succeeded' and row['output_id']
    assert 'MANAGED_STREAM_OUTPUT' in json.dumps(managed_store.get(g['db'],row['output_id']))
    assert g['db'].one('SELECT state FROM conversations')['state']=='active'


def test_interrupted_stream_retains_partial_and_requires_explicit_recovery(gateway):
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"PARTIAL_TO_RECOVER"}\n\n'
            raise httpx.ReadError('synthetic stream interruption')
    g=configured(gateway);g['app'].state.upstream_transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Interrupted()))
    c=conversation(g,protocol='responses');headers={**g['headers'],'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':c['id'],'X-Gateway-Context-Revision':'0','Idempotency-Key':'partial-once'}
    r=g['client'].post('/v1/responses',headers=headers,json={'model':'qa-model','input':'question','stream':True,'max_output_tokens':64})
    assert 'stream_interrupted' in r.text
    row=g['db'].one("SELECT * FROM conversation_turns WHERE kind='answer'")
    assert row['status']=='unknown' and 'PARTIAL_TO_RECOVER' in json.dumps(managed_store.get(g['db'],row['output_id']))
    assert g['db'].one('SELECT state FROM conversations')['state']=='needs_recovery'
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==1


def test_office_documents_scanned_pdf_and_encrypted_backup(gateway,tmp_path):
    from docx import Document
    from openpyxl import Workbook
    from aigateway.cli import backup,verify
    from pathlib import Path
    g=configured(gateway);files=[]
    doc=Document();doc.add_paragraph('DOCX_SALES_EVIDENCE');raw=io.BytesIO();doc.save(raw);files.append(('contract.docx',raw.getvalue()))
    book=Workbook();book.active.append(['XLSX_SALES_EVIDENCE',42]);raw=io.BytesIO();book.save(raw);files.append(('sales.xlsx',raw.getvalue()))
    raw=io.BytesIO();Image.new('RGB',(100,100),'white').save(raw,format='PDF');files.append(('scan.pdf',raw.getvalue()))
    ids=[]
    for name,data in files:
        r=g['client'].post('/v1/files',headers=g['headers'],files={'file':(name,data)})
        assert r.status_code==201,r.text;ids.append(r.json()['id'])
        meta=g['client'].get('/v1/files/'+ids[-1],headers=g['headers']).json();assert meta['sections']
    assert g['client'].get('/v1/files/'+ids[-1],headers=g['headers']).json()['sections'][0]['image']
    snapshot=backup(g['db'],source=Path(__file__).resolve().parents[1],root=tmp_path/'snapshots')
    result=verify(snapshot,tmp_path/'restored')
    assert result['managed_chunks_decrypted']>=6 and result['ledger_after']['ok']
    assert b'DOCX_SALES_EVIDENCE' not in (snapshot/'data/gateway.sqlite3').read_bytes()


def test_ordinary_user_cannot_operate_sessions_and_revoked_key_cannot_read(gateway):
    from fastapi.testclient import TestClient
    from conftest import PASSWORD,HEADERS
    g=configured(gateway);c=conversation(g);assert send(g,c).status_code==200
    other=g['client'].post('/api/v1/admin/users',json={'username':'session-other','display_name':'会话普通用户','password':PASSWORD});assert other.status_code==201
    client=TestClient(g['app']);client.headers.update(HEADERS);assert client.post('/api/v1/auth/login',json={'username':'session-other','password':PASSWORD}).status_code==200
    for path in ('/api/v1/admin/conversations','/api/v1/admin/conversations/'+c['id'],'/api/v1/admin/conversation-settings','/api/v1/admin/gateway-files'):
        assert client.get(path).status_code==403
    assert g['client'].delete('/api/v1/keys/'+g['key_id']).status_code==200
    assert g['client'].get('/api/v1/conversations/'+c['id'],headers=g['headers']).status_code==401


def test_authorization_version_excludes_old_external_context_and_files(gateway):
    g=configured(gateway);g['client'].put('/api/v1/admin/conversation-policies',json={'application_id':g['application'],'policy':'continuity','identity_mode':'application_subject'})
    a={**g,'headers':{**g['headers'],'X-Gateway-Subject':'same-user','X-Gateway-Authorization-Version':'scope-v1'}}
    b={**g,'headers':{**g['headers'],'X-Gateway-Subject':'same-user','X-Gateway-Authorization-Version':'scope-v2'}}
    c=conversation(a);assert send(a,c,'project A private data').status_code==200
    assert conversation(b)['id']!=c['id']
    assert g['client'].get('/api/v1/conversations/'+c['id'],headers=b['headers']).status_code==404


def test_client_can_restore_observed_response_without_replaying_model(gateway):
    g=configured(gateway);c=conversation(g);assert send(g,c,'lost response question').status_code==200
    g['db'].execute("UPDATE conversation_turns SET status='unknown' WHERE conversation_id=?",(c['id'],))
    g['db'].execute("UPDATE conversations SET state='needs_recovery' WHERE id=?",(c['id'],))
    before=g['db'].one('SELECT count(*) n FROM requests')['n']
    r=g['client'].post('/api/v1/conversations/'+c['id']+'/recover',headers=g['headers'],json={'acknowledge_unknown':True,'response':chat_result()})
    assert r.status_code==200 and r.json()['source']=='client_restored'
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==before
    assert g['db'].one("SELECT count(*) n FROM conversation_turns WHERE kind='recovery' AND status='client_restored'")['n']==1
    assert send(g,conversation(g)).status_code==200 and ledger_check(g['db'])['ok']


def test_output_storage_failure_returns_recovery_signal_without_replaying(gateway,monkeypatch):
    g=configured(gateway);c=conversation(g);original=managed_store.put
    def fail_output(db,value,kind='context',**kw):
        if kind=='context' and isinstance(value,dict) and 'choices' in value:raise OSError('synthetic storage failure')
        return original(db,value,kind,**kw)
    monkeypatch.setattr(managed_store,'put',fail_output)
    r=send(g,c);assert r.status_code==200 and r.headers['x-gateway-context-warning']=='needs_recovery'
    assert g['db'].one('SELECT status FROM requests')['status']=='succeeded'
    assert g['db'].one('SELECT state FROM conversations')['state']=='needs_recovery'
    assert g['db'].one('SELECT reserved_bytes FROM managed_settings')['reserved_bytes']==0
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==1 and ledger_check(g['db'])['ok']


def test_parallel_turn_rejected_while_first_call_owns_lease(gateway):
    import asyncio,threading
    from concurrent.futures import ThreadPoolExecutor
    g=configured(gateway);c=conversation(g);entered=threading.Event();release=threading.Event()
    async def provider(request):
        entered.set()
        while not release.is_set():await asyncio.sleep(.02)
        return httpx.Response(200,json=chat_result())
    g['app'].state.upstream_transport=httpx.MockTransport(provider)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(send,g,c,'first parallel question');assert entered.wait(3)
        try:
            rejected=send(g,c,'second parallel question');assert rejected.status_code==409
        finally:release.set()
        assert future.result(timeout=5).status_code==200
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==1


def test_unicode_turn_content_pages_and_cancelled_tool_pair(gateway):
    g=configured(gateway);g['db'].execute('UPDATE models SET context_limit=2000000');c=conversation(g)
    value='中文𐐷'*22000;r=send(g,c,value);assert r.status_code==200,r.text
    turn=g['db'].one("SELECT id FROM conversation_turns WHERE kind='answer'")['id'];cursor=0;text=''
    while cursor is not None:
        page=g['client'].get('/api/v1/conversations/'+c['id']+'/turns/'+turn+'/content',headers=g['headers'],params={'cursor':cursor}).json()
        text+=page['text'];cursor=page['next_cursor']
    assert json.loads(text)['items'][0]['content']==value
    assert b'\xe4\xb8\xad\xe6\x96\x87' not in g['db'].path.read_bytes()


def test_zip_expansion_guard_and_pdf_page_selection(gateway):
    import zipfile
    from pypdf import PdfWriter
    g=configured(gateway);bad=io.BytesIO()
    with zipfile.ZipFile(bad,'w') as z:
        for i in range(2001):z.writestr('p'+str(i),'')
    r=g['client'].post('/v1/files',headers=g['headers'],files={'file':('bad.docx',bad.getvalue())});assert r.status_code==400
    pdf=PdfWriter()
    for _ in range(101):pdf.add_blank_page(width=100,height=100)
    raw=io.BytesIO();pdf.write(raw)
    uploaded=g['client'].post('/v1/files',headers=g['headers'],files={'file':('long.pdf',raw.getvalue())});assert uploaded.status_code==201
    pk=uploaded.json()['id'];metadata=g['client'].get('/v1/files/'+pk,headers=g['headers']).json()
    assert metadata['status']=='needs_selection' and metadata['page_count']==101
    assert g['client'].put('/api/v1/admin/model-media/'+g['model'],json={'vision':True,'verified':True,'image_tokens':2048,'max_images':4}).status_code==200
    c=conversation(g);r=send(g,c,messages=[{'role':'user','content':[{'type':'file','file':{'file_id':pk,'selected_sections':['page:101']}}]}])
    assert r.status_code==200,r.text


def test_managed_chat_resource_stickiness_and_strict_policy(gateway):
    g=configured(gateway)
    second=g['client'].post('/api/v1/admin/upstreams',json={'name':'second resource','pool_id':g['pool'],'provider':'compatible','base_url':'https://second.test/v1','api_key':'SYNTHETIC_SECOND_KEY','enabled':True}).json()['id']
    c=conversation(g);assert send(g,c,'resource one').status_code==200
    bound=g['db'].one('SELECT gateway_upstream FROM conversations WHERE id=?',(c['id'],))['gateway_upstream']
    assert send(g,conversation(g),'resource two').status_code==200
    assert g['db'].one('SELECT upstream_id FROM requests ORDER BY created_at DESC LIMIT 1')['upstream_id']==bound
    g['db'].execute('UPDATE upstreams SET enabled=0 WHERE id=?',(bound,))
    assert send(g,conversation(g),'resource continuation').status_code==200
    assert g['db'].one('SELECT upstream_id FROM requests ORDER BY created_at DESC LIMIT 1')['upstream_id']!=bound
    g['client'].put('/api/v1/admin/conversation-policies',json={'application_id':g['application'],'task_type':'strict_resource','policy':'strict'})
    c=conversation(g,task_type='strict_resource');assert send(g,c).status_code==200
    bound=g['db'].one('SELECT gateway_upstream FROM conversations WHERE id=?',(c['id'],))['gateway_upstream'];g['db'].execute('UPDATE upstreams SET enabled=0 WHERE id=?',(bound,))
    count=g['db'].one('SELECT count(*) n FROM requests')['n'];r=send(g,conversation(g,task_type='strict_resource'))
    assert r.status_code==503 and r.json()['error']['code']=='session_upstream_unavailable'
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==count


def test_route_change_invalidates_media_attestation_and_policy_change_is_explicit(gateway):
    from aigateway.models import ModelInput
    g=configured(gateway);c=conversation(g)
    assert g['client'].put('/api/v1/admin/model-media/'+g['model'],json={'vision':True,'verified':True,'image_tokens':4096,'max_images':4}).status_code==200
    row=g['client'].get('/api/v1/admin/models').json()[0];body={k:row[k] for k in ModelInput.model_fields if k in row};body['enabled']=bool(body['enabled']);body['upstream_model']='new-unverified-model'
    assert g['client'].put('/api/v1/admin/models/'+g['model'],json=body).status_code==200
    assert g['db'].one('SELECT vision,verified FROM model_media')=={'vision':0,'verified':0}
    assert g['client'].post('/api/v1/admin/conversations/'+c['id']+'/policy',json={'policy':'strict','acknowledge_policy_change':True}).status_code==200
    current=conversation(g);assert current['policy']=='strict' and current['revision']==1


def test_small_text_attachment_followup_does_not_charge_unneeded_summary(gateway):
    g=configured(gateway);file=g['client'].post('/v1/files',headers=g['headers'],files={'file':('small.txt',b'Short sales evidence')}).json()['id']
    c=conversation(g);assert send(g,c,messages=[{'role':'user','content':[{'type':'text','text':'analyze'},{'type':'file','file':{'file_id':file}}]}]).status_code==200
    assert send(g,conversation(g),'follow up').status_code==200
    assert g['db'].one("SELECT count(*) n FROM conversation_turns WHERE kind='summary'")['n']==0
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==2


def test_managed_audit_failure_does_not_break_financial_binding(gateway,monkeypatch):
    from aigateway.request_audit import AuditContext
    g=configured(gateway);c=conversation(g)
    monkeypatch.setattr(AuditContext,'associate',lambda *a,**kw:(_ for _ in ()).throw(OSError('synthetic metadata outage')))
    r=send(g,c);assert r.status_code==200,r.text
    request=g['db'].one('SELECT id,status FROM requests');turn=g['db'].one("SELECT request_id,status FROM conversation_turns WHERE kind='answer'")
    assert turn['request_id']==request['id'] and turn['status']==request['status']=='succeeded'
    assert ledger_check(g['db'])['ok']


def test_turn_idempotency_can_be_queried_after_lost_response(gateway):
    g=configured(gateway);c=conversation(g)
    headers={**g['headers'],'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':c['id'],'X-Gateway-Context-Revision':'0','Idempotency-Key':'stable-web-operation'}
    body={'model':'qa-model','messages':[{'role':'user','content':'idempotent website question'}],'max_tokens':64}
    first=g['client'].post('/v1/chat/completions',headers=headers,json=body);assert first.status_code==200
    headers['X-Gateway-Context-Revision']='1'
    duplicate=g['client'].post('/v1/chat/completions',headers=headers,json=body);assert duplicate.status_code==409
    assert duplicate.json()['error']['request_id']==first.headers['x-request-id']
    found=g['client'].get('/api/v1/conversations/'+c['id']+'/turns',headers=g['headers'],params={'operation_id':'stable-web-operation'}).json()['items']
    assert len(found)==1 and found[0]['request_id']==first.headers['x-request-id']
    assert g['db'].one('SELECT count(*) n FROM requests')['n']==1

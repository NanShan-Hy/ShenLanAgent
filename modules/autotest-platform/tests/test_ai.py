import json
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI,Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from conftest import serve,PASSWORD
from testdock.ai_runtime import AIRuntime
from testdock.ai_gateway import GatewayFailure
from testdock.core import password_hash

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'ai-workbench/src'))
from aiworkbench.core import Settings as WBSettings,Database as WBDatabase,password_hash as wb_hash
from aiworkbench.app import create_app as wb_app
from aiworkbench.access import create_record,Access
from aiworkbench.retrieval import index_record


@pytest.fixture
def connected(platform,tmp_path,monkeypatch):
    c,db,s,app=platform;key=tmp_path/'bridge.key';key.write_text('isolated-integration-key');key.chmod(0o600)
    monkeypatch.setenv('TESTDOCK_AI_BRIDGE_KEY_FILE',str(key))
    with serve(app) as tdurl:
        settings=WBSettings(tmp_path/'wbdata',tmp_path/'wbconfig/key',bridge_key_file=key,testdock_url=tdurl,min_free_bytes=0)
        wb=WBDatabase(settings);wb.initialize()
        wb.execute('INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)',('wbadmin','独立工作台管理员',wb_hash(PASSWORD),1,time.time()))
        with serve(wb_app(settings)) as wburl:
            r=c.put('/api/ai/settings',json={'workbench_url':wburl});assert r.status_code==200,r.text
            context=c.get('/api/ai/context');assert context.status_code==200,context.text
            yield c,db,s,app,wb,context.json()


class ProtocolGateway:
    def __init__(self):
        self.app=FastAPI();self.calls=[];self.sessions={};self.outputs=[];self.files={};self.omit_completed_output=False
        @self.app.get('/v1/models')
        def models():return {'object':'list','data':[{'id':'contract-model','object':'model'}]}
        @self.app.post('/api/v1/conversations/resolve')
        async def resolve(request:Request):
            body=await request.json();pk='session-'+str(len(self.sessions));self.sessions[pk]={'id':pk,'revision':1,'state':'active','pending_calls':[]}
            return self.sessions[pk]
        @self.app.get('/api/v1/conversations/{pk}')
        def detail(pk:str):return self.sessions[pk]
        @self.app.post('/api/v1/conversations/{pk}/recover')
        def recover(pk:str):self.sessions[pk]['state']='active';return self.sessions[pk]
        @self.app.post('/api/v1/conversations/{pk}/cancel-tools')
        def cancel(pk:str):self.sessions[pk]['pending_calls']=[];return self.sessions[pk]
        @self.app.delete('/api/v1/conversations/{pk}')
        def delete(pk:str):return {'deleted':True}
        @self.app.post('/v1/files')
        async def upload(request:Request):
            async with request.form() as form:
                raw=await form['file'].read();pk='file-'+str(len(self.files));self.files[pk]={'id':pk,'status':'ready','bytes':len(raw),'sections':[]}
            return self.files[pk]
        @self.app.get('/v1/files/{pk}')
        def info(pk:str):return self.files[pk]
        @self.app.delete('/v1/files/{pk}')
        def delete_file(pk:str):return {'deleted':True}
        @self.app.post('/v1/responses')
        async def respond(request:Request):
            body=await request.json();self.calls.append({'body':body,'headers':dict(request.headers)})
            pk=request.headers['x-gateway-conversation-id'];session=self.sessions[pk]
            assert int(request.headers['x-gateway-context-revision'])==session['revision']
            session['revision']+=1
            output=self.outputs.pop(0) if self.outputs else [{'type':'message','content':[{'type':'output_text','text':'连接成功。'}]}]
            def stream():
                for item in output:
                    if item['type']=='function_call':
                        yield 'data: '+json.dumps({'type':'response.output_item.added','output_index':0,'item':{**item,'arguments':''}})+'\n\n'
                        yield 'data: '+json.dumps({'type':'response.function_call_arguments.delta','output_index':0,'delta':item['arguments']})+'\n\n'
                    for part in item.get('content',[]):yield 'data: '+json.dumps({'type':'response.output_text.delta','delta':part['text']},ensure_ascii=False)+'\n\n'
                final={'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}
                if not self.omit_completed_output:final['output']=output
                yield 'data: '+json.dumps({'type':'response.completed','response':final},ensure_ascii=False)+'\n\n'
            return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Gateway-Context-Revision':str(session['revision']),'X-Request-Id':'req-'+str(len(self.calls))})


@pytest.fixture
def generation(connected):
    c,db,s,app,wb,context=connected;gateway=ProtocolGateway()
    with serve(gateway.app) as url:
        settings=c.get('/api/ai/settings').json();settings.pop('key_configured');settings.update(enabled=True,api_key='isolated-platform-key',model='contract-model',base_url=url+'/v1')
        assert c.put('/api/ai/settings',json=settings).status_code==200
        yield connected,gateway


def conversation(c,**kwargs):
    r=c.post('/api/ai/conversations',json=kwargs);assert r.status_code==201,r.text;return r.json()['id']


def send(c,conv,**kwargs):
    r=c.post('/api/ai/conversations/'+conv+'/messages',json={'query':'帮助检查',**kwargs});assert r.status_code==201,r.text;return r.json()['id']


def policy(c,context):
    p=next(p for p in context['projects'] if p['external_id']==1)
    assert c.put('/api/ai/project-policy',json={'project_id':p['id'],'egress':'external'}).status_code==200


def test_session_identity_replay_logout_and_private_workbench(connected):
    c,db,s,app,wb,context=connected
    uid=context['identity']['id'];assert uid!=1
    assert wb.one('SELECT is_admin,password_hash FROM users WHERE id=?',(uid,))=={'is_admin':0,'password_hash':'!source-login-only'}
    assert len(wb.all('SELECT * FROM td_identities'))==1
    c.get('/api/ai/context');assert len(wb.all('SELECT * FROM td_identities'))==1
    assert wb.one('SELECT count(*) n FROM td_sessions')['n']==1
    private=create_record(wb,wb.one('SELECT * FROM users WHERE id=1'),{'kind':'document','title':'独立私人资料','body':'PRIVATE_WORKBENCH_SENTINEL','visibility':'private'})
    index_record(wb,private)
    assert not c.post('/api/ai/search',json={'query':'PRIVATE_WORKBENCH_SENTINEL'}).json()['items']
    ticket=db.one('SELECT * FROM ai_tickets');assert ticket['used']==1
    token=wb.one('SELECT * FROM td_sessions')
    assert 'isolated-integration-key' not in wb.path.read_bytes().decode(errors='ignore')
    c.post('/api/auth/logout',json={})
    from aiworkbench.access import Access
    user=wb.one('SELECT * FROM users WHERE id=?',(uid,))
    assert Access(wb,user,binding={'external_user_id':1,'secret':token['secret']}).upstream_unavailable


def test_revoked_identity_requires_explicit_reconnect(connected):
    c,db,s,app,wb,context=connected
    assert c.post('/api/ai/revoke',json={}).status_code==200
    assert c.get('/api/ai/context').status_code==403
    assert c.get('/api/ai/status').json()['revoked']
    assert c.post('/api/ai/reconnect',json={}).status_code==200
    assert c.get('/api/ai/context').status_code==200


def test_typed_actions_atomic_confirmation_and_native_routes(connected):
    c,db,s,app,wb,context=connected;conv=conversation(c)
    def execute(kind,args,target=None):
        r=c.post('/api/ai/conversations/'+conv+'/proposals',json={'operation':kind,'project_id':1,'target_id':target,'arguments':args});assert r.status_code==201,r.text
        pk=r.json()['id'];r=c.post('/api/ai/proposals/'+pk+'/confirm',json={});assert r.status_code==200,r.text
        again=c.post('/api/ai/proposals/'+pk+'/confirm',json={});assert again.status_code==200,again.text
        assert again.json()['result']==r.json()['result'];return r.json()
    case=execute('case.create',{'name':'AI新用例','steps':[{'action':'http','url':'/api/info'}]})['result'];assert case['id']
    execute('case.update',{'name':'AI修改用例','steps':[{'action':'http','url':'/api/info'}]},case['id'])
    suite=execute('suite.create',{'name':'AI套件','case_ids':[case['id']]})['result']
    execute('suite.update',{'name':'AI改套件','case_ids':[case['id']]},suite['id'])
    env=execute('environment.create',{'name':'AI环境','api_base_url':'http://127.0.0.1:8091'})['result']
    execute('environment.update',{'name':'AI环境改','api_base_url':'http://127.0.0.1:8091'},env['id'])
    schedule=execute('schedule.create',{'name':'AI计划','suite_id':suite['id'],'environment_id':env['id'],'cron':'0 9 * * *'})
    assert not schedule['arguments']['enabled'];assert len(schedule['next_fires'])==5
    execute('schedule.update',{'name':'AI修改计划','suite_id':suite['id'],'environment_id':env['id'],'cron':'0 10 * * *','enabled':False},schedule['result']['id'])
    execute('schedule.enable',{'enabled':True},schedule['result']['id'])
    run=execute('run.create',{'suite_id':suite['id'],'environment_id':env['id']})['result']
    execute('run.matrix',{'suite_id':suite['id'],'environment_ids':[1,2]})
    db.execute("UPDATE runs SET status='failed' WHERE id=?",(run['id'],))
    execute('run.rerun',{'failed_only':True,'use_latest':False},run['id'])
    assert db.one('SELECT count(*) n FROM runs')['n']==4
    assert db.one("SELECT count(*) n FROM audit_logs WHERE action='ai.proposal.apply'")['n']==12


def test_confirmation_change_expiry_schema_and_rollback(connected,monkeypatch):
    c,db,s,app,wb,context=connected;conv=conversation(c)
    def proposed():return c.post('/api/ai/conversations/'+conv+'/proposals',json={'operation':'case.create','project_id':1,'arguments':{'name':'待确认','steps':[{'action':'http','url':'/api/info'}]}}).json()['id']
    pk=proposed();db.execute("UPDATE cases SET name='并发编辑' WHERE id=1")
    assert c.post('/api/ai/proposals/'+pk+'/confirm',json={}).status_code==409
    pk=proposed();db.execute('UPDATE ai_proposals SET expires_at=0 WHERE id=?',(pk,));assert c.post('/api/ai/proposals/'+pk+'/confirm',json={}).status_code==409
    assert c.post('/api/ai/conversations/'+conv+'/proposals',json={'operation':'environment.update','project_id':1,'target_id':1,'arguments':{'name':'bad','secrets':{'password':'forbidden'}}}).status_code==422
    assert c.post('/api/ai/conversations/'+conv+'/proposals',json={'operation':'case.create','project_id':1,'arguments':{'name':'bad','steps':[]}}).status_code==422
    pk=proposed();before=db.one('SELECT count(*) n FROM cases')['n']
    import testdock.ai_actions as actions
    real=actions.execute_native
    def failing(*args,**kwargs):real(*args,**kwargs);raise RuntimeError('simulated commit boundary failure')
    monkeypatch.setattr(actions,'execute_native',failing)
    with pytest.raises(RuntimeError):c.post('/api/ai/proposals/'+pk+'/confirm',json={})
    assert db.one('SELECT count(*) n FROM cases')['n']==before
    assert db.one('SELECT status FROM ai_proposals WHERE id=?',(pk,))['status']=='pending'


def test_repeated_applied_update_returns_result_after_its_source_changes(connected):
    c,db,s,app,wb,context=connected;conv=conversation(c)
    ref=c.post('/api/ai/source',json={'system':'testdock','kind':'cases','id':1}).json()
    p=c.post('/api/ai/conversations/'+conv+'/proposals',json={'operation':'case.update','project_id':1,'target_id':1,'arguments':{'name':'已确认更新','steps':[{'action':'http','url':'/api/info'}]},'sources':[ref]})
    assert p.status_code==201,p.text;pk=p.json()['id']
    applied=c.post('/api/ai/proposals/'+pk+'/confirm',json={});assert applied.status_code==200,applied.text
    repeated=c.post('/api/ai/proposals/'+pk+'/confirm',json={});assert repeated.status_code==200,repeated.text
    assert repeated.json()['result']==applied.json()['result']
    assert db.one('SELECT revision FROM cases WHERE id=1')['revision']==2
    result=c.get('/api/ai/conversations/'+conv).json()['proposals'][0]
    assert result['status']=='applied' and not result.get('sources')


def test_gateway_managed_tool_loop_history_and_unknown(generation,monkeypatch):
    connected,gateway=generation;c,db,s,app,wb,context=connected;policy(c,context);conv=conversation(c)
    gateway.outputs=[
        [{'type':'function_call','name':'testdock_read','call_id':'read-1','arguments':json.dumps({'project_id':1,'kind':'cases','id':1})}],
        [{'type':'message','content':[{'type':'output_text','text':'该用例包含页面登录检查 [C1]。'}]}]]
    tid=send(c,conv);assert AIRuntime(db).run_once();view=c.get('/api/ai/tasks/'+tid).json();assert view['status']=='completed',view
    assert len(gateway.calls)==2
    assert gateway.calls[1]['body']['input'][0]['type']=='function_call_output'
    assert gateway.calls[0]['headers']['x-gateway-subject']==gateway.calls[1]['headers']['x-gateway-subject']
    assert gateway.calls[0]['headers']['idempotency-key']!=gateway.calls[1]['headers']['idempotency-key']
    assert len(c.get('/api/ai/conversations/'+conv).json()['messages'])==2
    gateway.outputs=[[{'type':'message','content':[{'type':'output_text','text':'结合刚才的用例 [C1]，可以继续检查。'}]}]]
    send(c,conv,query='继续');AIRuntime(db).run_once();assert len(gateway.sessions)==1
    from testdock.ai_gateway import Gateway
    def unknown(*args,**kwargs):raise GatewayFailure('upstream unknown',True)
    monkeypatch.setattr(Gateway,'generate',unknown)
    tid=send(c,conv,query='未知结果');AIRuntime(db).run_once()
    assert c.get('/api/ai/tasks/'+tid).json()['status']=='unknown'
    assert not AIRuntime(db).run_once()
    assert c.post('/api/ai/conversations/'+conv+'/recover',json={'acknowledge_unknown':True}).status_code==200
    assert c.get('/api/ai/tasks/'+tid).json()['status']=='cancelled'


def test_tool_proposal_waits_for_confirmation_then_resumes(generation):
    connected,gateway=generation;c,db,s,app,wb,context=connected;conv=conversation(c);before=db.one('SELECT count(*) n FROM cases')['n']
    gateway.outputs=[[{'type':'function_call','name':'prepare_action','call_id':'write-1','arguments':json.dumps({'operation':'case.create','project_id':1,'arguments':{'name':'确认后新增','steps':[{'action':'http','url':'/api/info'}]}})}]]
    tid=send(c,conv);AIRuntime(db).run_once();assert c.get('/api/ai/tasks/'+tid).json()['status']=='waiting_confirmation'
    assert db.one('SELECT count(*) n FROM cases')['n']==before
    p=c.get('/api/ai/conversations/'+conv).json()['proposals'][0]
    assert c.post('/api/ai/proposals/'+p['id']+'/confirm',json={}).status_code==200
    AIRuntime(db).run_once();assert db.one('SELECT count(*) n FROM cases')['n']==before+1
    assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed'
    assert gateway.calls[1]['body']['input'][0]['call_id']=='write-1'


def test_attachments_consent_encryption_upload_and_cleanup(generation):
    connected,gateway=generation;c,db,s,app,wb,context=connected;conv=conversation(c);policy(c,context)
    r=c.post('/api/ai/conversations/'+conv+'/files',data={'project_id':'1','external_consent':'false'},files={'file':('notes.txt',b'PRIVATE_FILE_SENTINEL')});assert r.status_code==201,r.text
    fid=r.json()['id'];assert b'PRIVATE_FILE_SENTINEL' not in (s.data_dir/'ai'/fid).read_bytes()
    tid=send(c,conv,file_ids=[fid]);AIRuntime(db).run_once();assert c.get('/api/ai/tasks/'+tid).json()['status']=='failed';assert not gateway.files
    assert c.put('/api/ai/files/'+fid,json={'external_consent':True,'selected_sections':[]}).status_code==200
    gateway.outputs=[[{'type':'message','content':[{'type':'output_text','text':'附件内容已核对 [C1]。'}]}]]
    tid=send(c,conv,file_ids=[fid]);AIRuntime(db).run_once();assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed'
    assert len(gateway.files)==1
    assert any(p['type']=='input_file' for p in gateway.calls[0]['body']['input'][-1]['content'])
    assert c.get('/api/ai/files/'+fid+'/content').content==b'PRIVATE_FILE_SENTINEL'
    assert c.delete('/api/ai/conversations/'+conv).status_code==200
    assert not (s.data_dir/'ai'/fid).exists()
    assert c.get('/api/ai/files/'+fid).status_code==404


def test_source_version_and_egress_change_start_new_context(generation):
    connected,gateway=generation;c,db,s,app,wb,context=connected;policy(c,context);conv=conversation(c)
    ref=c.post('/api/ai/source',json={'system':'testdock','kind':'cases','id':1}).json()
    gateway.outputs=[[{'type':'message','content':[{'type':'output_text','text':'用例证据 [C1]。'}]}]]
    tid=send(c,conv,sources=[ref]);AIRuntime(db).run_once();assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed'
    db.execute("UPDATE cases SET name='更新过的用例' WHERE id=1")
    send(c,conv,query='你好');AIRuntime(db).run_once();assert len(gateway.sessions)==2
    assert c.get('/api/ai/conversations/'+conv).json()['hidden_messages']==2


def test_private_conversations_permissions_and_secret_rejection(connected):
    c,db,s,app,wb,context=connected;conv=conversation(c,title='PRIVATE_TITLE_SENTINEL')
    uid=db.execute('INSERT INTO users(username,display_name,password_hash,created_at) VALUES(?,?,?,?)',('viewer','只读',password_hash(PASSWORD),time.time()))
    db.execute("INSERT INTO members VALUES(?,?,'viewer')",(1,uid))
    other=TestClient(app,headers={'X-Requested-With':'TestDock'});other.post('/api/auth/login',json={'username':'viewer','password':PASSWORD})
    assert other.get('/api/ai/conversations/'+conv).status_code==404
    cv=conversation(other)
    assert other.post('/api/ai/conversations/'+cv+'/proposals',json={'operation':'case.create','project_id':1,'arguments':{'name':'x','steps':[{'action':'http','url':'/'}]}}).status_code==403
    assert other.put('/api/ai/settings',json={}).status_code==403
    assert not c.get('/api/ai/settings').json()['key_configured']
    assert b'PRIVATE_TITLE_SENTINEL' not in db.path.read_bytes()
    other.close()


def test_stream_reconstruction_without_complete_output(generation):
    connected,gateway=generation;c,db,s,app,wb,context=connected;policy(c,context);conv=conversation(c)
    gateway.omit_completed_output=True
    gateway.outputs=[[{'type':'function_call','name':'testdock_read','call_id':'delta-only','arguments':json.dumps({'project_id':1,'kind':'cases','id':1})}],
                     [{'type':'message','content':[{'type':'output_text','text':'增量重组后的依据 [C1]。'}]}]]
    tid=send(c,conv);AIRuntime(db).run_once()
    assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed',db.one('SELECT error FROM ai_tasks WHERE id=?',(tid,))


def test_memory_candidate_review_policy_and_realtime_revocation(connected):
    c,db,s,app,wb,context=connected;uid=context['identity']['id'];user=wb.one('SELECT * FROM users WHERE id=?',(uid,))
    source=create_record(wb,user,{'kind':'document','title':'记忆来源','body':'记忆来源中的已确认事实','visibility':'private','egress':'local'})
    index_record(wb,source)
    assert c.post('/api/ai/search',json={'query':'已确认事实'}).json()['items']
    r=c.post('/api/ai/memory',json={'title':'候选约定','body':'已确认事实','sources':[{'id':source,'version':1}]});assert r.status_code==200,r.text
    rid=r.json()['id'];assert wb.one('SELECT status FROM records WHERE id=?',(rid,))['status']=='pending'
    assert c.post('/api/ai/memory',json={'action':'view','id':rid}).json()['status']=='pending'
    assert c.post('/api/ai/memory',json={'action':'review','id':rid,'version':1,'decision':'approve'}).status_code==200
    assert wb.one('SELECT status FROM records WHERE id=?',(rid,))['status']=='approved'
    assert c.put('/api/ai/source-policy',json={'id':source,'egress':'external'}).status_code==200
    assert c.put('/api/ai/source-policy',json={'id':rid,'egress':'external'}).status_code==200
    assert c.post('/api/ai/source',json={'system':'workbench','id':rid,'version':1}).status_code==200
    wb.execute('UPDATE records SET version=version+1 WHERE id=?',(source,))
    assert c.post('/api/ai/source',json={'system':'workbench','id':rid,'version':1}).status_code==409


def test_signature_replay_rejects_before_identity_lookup(connected):
    import hashlib,hmac,httpx
    from testdock.ai_store import config
    c,db,s,app,wb,context=connected
    conn=db.one('SELECT * FROM ai_connections');token=db.settings.unseal(conn['secret'])['token']
    raw=b'{}';ts=str(time.time());nonce='test-replay-nonce-12345';path='/api/integrations/testdock/context'
    signature=hmac.new(b'isolated-integration-key',(ts+'\n'+nonce+'\n'+path+'\n'+hashlib.sha256(raw).hexdigest()).encode(),hashlib.sha256).hexdigest()
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','X-AI-Timestamp':ts,'X-AI-Nonce':nonce,'X-AI-Signature':signature}
    with httpx.Client(trust_env=False) as client:
        assert client.post(config(db)['workbench_url']+path,content=raw,headers=headers).status_code==200
        assert client.post(config(db)['workbench_url']+path,content=raw,headers=headers).status_code==401


def test_ai_backup_restore_disables_model_grants_and_proposals(generation,tmp_path):
    import sqlite3,importlib.util
    from testdock.cli import backup
    connected,gateway=generation;c,db,s,app,wb,context=connected;conv=conversation(c)
    c.post('/api/ai/conversations/'+conv+'/files',data={'project_id':'1'},files={'file':('recovery.txt',b'RESTORE_PRIVATE_FILE')})
    send(c,conv)
    p=c.post('/api/ai/conversations/'+conv+'/proposals',json={'operation':'case.create','project_id':1,'arguments':{'name':'恢复后不得执行','steps':[{'action':'http','url':'/'}]}});assert p.status_code==201
    snapshot=backup(db,tmp_path/'backups')
    spec=importlib.util.spec_from_file_location('ai_backup_verifier',Path(__file__).resolve().parents[1]/'scripts/verify_backup.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    output=tmp_path/'restore';m.verify(snapshot,output)
    from cryptography.fernet import Fernet
    cipher=Fernet((output/'master.key').read_bytes())
    with sqlite3.connect(output/'testdock.sqlite3') as conn:
        assert not json.loads(cipher.decrypt(conn.execute('SELECT secret FROM ai_settings').fetchone()[0].encode()))['enabled']
        assert conn.execute('SELECT count(*) FROM ai_source_grants').fetchone()[0]==0
        assert conn.execute('SELECT status FROM ai_tasks').fetchone()[0]=='cancelled'
        assert conn.execute('SELECT status FROM ai_proposals').fetchone()[0]=='rejected'
        assert conn.execute("SELECT value FROM ai_meta WHERE name='installation'").fetchone()[0]!=db.one("SELECT value FROM ai_meta WHERE name='installation'")['value']
    assert db.one('SELECT status FROM ai_tasks')['status']=='queued'


def test_missing_selected_scope_never_falls_back_to_global_search(connected):
    from fastapi import HTTPException
    c,db,s,app,wb,context=connected
    class RevokedKnowledge:
        def call(self,action,body=None):
            assert action=='context','must not search another project after scope revocation'
            return {'projects':[{'id':2,'external_id':2,'egress':'external'}]}
    with pytest.raises(HTTPException) as error:
        AIRuntime(db).execute_tool('knowledge_search',{'query':'secret'}, {},{'sources':[]},db.one('SELECT * FROM users WHERE id=1'),RevokedKnowledge(),{'mode':'project','project_id':1})
    assert error.value.status_code==403


def test_project_revoked_before_dispatch_makes_no_model_request(generation):
    connected,gateway=generation;c,db,s,app,wb,context=connected
    conv=conversation(c,mode='project',project_id=1);tid=send(c,conv)
    db.execute('UPDATE users SET is_admin=0 WHERE id=1');db.execute('DELETE FROM members WHERE user_id=1 AND project_id=1')
    AIRuntime(db).run_once()
    assert db.one('SELECT status FROM ai_tasks WHERE id=?',(tid,))['status']=='failed'
    assert not gateway.calls


def test_explicit_sources_cannot_expand_fixed_project_scope(generation):
    connected,gateway=generation;c,db,s,app,wb,context=connected
    other=c.post('/api/projects',json={'name':'另一个有权项目'}).json()['id']
    case=c.post('/api/projects/'+str(other)+'/cases',json={'name':'范围外用例','steps':[{'action':'http','url':'/'}]}).json()['id']
    ref=c.post('/api/ai/source',json={'system':'testdock','kind':'cases','id':case}).json()
    conv=conversation(c,mode='project',project_id=1)
    r=c.post('/api/ai/conversations/'+conv+'/messages',json={'query':'检查资料','sources':[ref]})
    assert r.status_code==403,r.text
    assert not db.one('SELECT id FROM ai_tasks') and not gateway.calls


def test_cross_project_derived_knowledge_is_filtered_from_project_scope(connected):
    c,db,s,app,wb,context=connected
    class KnowledgeWithMixedSources:
        def call(self,action,body=None):
            if action=='context':return {'projects':[{'id':1,'external_id':1},{'id':2,'external_id':2}]}
            return {'mode':'local-keyword','items':[{'system':'workbench','id':'mixed','project_id':1,'deps':[1,2],'version':1,'text':'cross-project fact'}]}
    payload={'sources':[]}
    result=AIRuntime(db).execute_tool('knowledge_search',{'query':'fact'},{},payload,db.one('SELECT * FROM users WHERE id=1'),KnowledgeWithMixedSources(),{'mode':'project','project_id':1})
    assert not result['sources'] and not payload['sources']

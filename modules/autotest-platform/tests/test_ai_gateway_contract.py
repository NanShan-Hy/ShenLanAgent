"""Run with the existing Gateway venv: real Gateway code, synthetic upstream and isolated ledgers."""
import io
import json
import sys
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from conftest import serve
from test_ai import connected,conversation,send,policy
from testdock.ai_runtime import AIRuntime

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'ai-gateway/src'))
pytest.importorskip('PIL',exc_type=ImportError)
pytest.importorskip('aigateway.app',exc_type=ImportError)
from aigateway.app import create_app
from aigateway.core import Settings,Database,create_user
from aigateway.billing import ledger_check


def test_actual_gateway_responses_tools_continuation_media_and_finance(connected,tmp_path):
    c,db,s,app,wb,context=connected;policy(c,context);seen=[]
    def upstream(request):
        body=json.loads(request.content);seen.append(body)
        if len(seen)==1:
            output=[{'type':'function_call','id':'fc-native','call_id':'call-native','name':'testdock_read','arguments':json.dumps({'project_id':1,'kind':'cases','id':1})}]
        else:output=[{'type':'message','id':'msg-native','role':'assistant','content':[{'type':'output_text','text':'已依据授权的测试资料完成分析 [C1]。'}]}]
        response={'id':'resp-'+str(len(seen)),'object':'response','status':'completed','output':output,
                  'usage':{'input_tokens':100,'output_tokens':30,'total_tokens':130,'input_tokens_details':{'cached_tokens':0}}}
        events=[{'type':'response.created','response':{'id':response['id'],'status':'in_progress'}}]
        for i,item in enumerate(output):
            events.append({'type':'response.output_item.added','output_index':i,'item':item})
            if item['type']=='message':events.append({'type':'response.output_text.delta','output_index':i,'content_index':0,'delta':item['content'][0]['text']})
            events.append({'type':'response.output_item.done','output_index':i,'item':item})
        events.append({'type':'response.completed','response':response})
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},content=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events))
    settings=Settings(tmp_path/'gateway/data',tmp_path/'gateway/config/key',public_url='http://testserver',allow_private_upstreams=True,min_free_bytes=0)
    gd=Database(settings);gd.initialize()
    with gd.transaction() as conn:uid,tenant,application=create_user(conn,'admin','合成联调管理员','synthetic-password-123',admin=True)
    ga=create_app(settings,upstream_transport=httpx.MockTransport(upstream))
    gc=TestClient(ga,headers={'X-Requested-With':'AIGateway'})
    assert gc.post('/api/v1/auth/login',json={'username':'admin','password':'synthetic-password-123'}).status_code==200
    pool=gc.post('/api/v1/admin/pools',json={'name':'合成协议池','quota_group':'integration-local','enabled':True,'rpm':1000,'tpm':10000000,'concurrency':10}).json()['id']
    assert gc.post('/api/v1/admin/upstreams',json={'name':'受控本地协议','pool_id':pool,'provider':'compatible','base_url':'https://protocol.invalid/v1','api_key':'synthetic-upstream','enabled':True}).status_code==201
    model=gc.post('/api/v1/admin/models',json={'alias':'qa-model','pool_id':pool,'provider':'compatible','upstream_model':'synthetic-model','protocols':['responses'],'enabled':True,'input_rate':1000000,'cache_rate':500000,'output_rate':2000000}).json()['id']
    key=gc.post('/api/v1/keys',json={'name':'合成测试Key','application_id':application}).json()['key']
    assert gc.post('/api/v1/admin/grants',json={'tenant_id':tenant,'amount_micro':1000000000,'client_ref':'isolated-testdock-contract','note':'仅隔离测试的虚拟额度'}).status_code==201
    assert gc.put('/api/v1/admin/policies/user/'+uid,json={'rpm':1000,'tpm':10000000,'daily_tokens':10000000,'concurrency':10}).status_code==200
    assert gc.put('/api/v1/admin/conversation-policies',json={'application_id':application,'identity_mode':'application_subject','policy':'continuity'}).status_code==200
    assert gc.put('/api/v1/admin/model-media/'+model,json={'vision':True,'verified':True,'image_tokens':2048,'max_images':4}).status_code==200
    with serve(ga) as url:
        profile=c.get('/api/ai/settings').json();profile.pop('key_configured');profile.update(enabled=True,base_url=url+'/v1',api_key=key,model='qa-model',vision_models=['qa-model'])
        assert c.put('/api/ai/settings',json=profile).status_code==200
        conv=conversation(c);tid=send(c,conv);AIRuntime(db).run_once()
        assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed',db.one('SELECT status,error FROM ai_tasks WHERE id=?',(tid,))
        assert len(seen)==2
        tid=send(c,conv,query='继续分析');AIRuntime(db).run_once()
        assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed',db.one('SELECT status,error FROM ai_tasks WHERE id=?',(tid,))
        from PIL import Image
        buf=io.BytesIO();Image.new('RGB',(20,20),'blue').save(buf,format='PNG')
        ids=[]
        for name,raw in [('attachment.txt',b'ISOLATED_ATTACHMENT_TEXT'),('screenshot.png',buf.getvalue())]:
            r=c.post('/api/ai/conversations/'+conv+'/files',data={'project_id':'1','external_consent':'true'},files={'file':(name,raw)});assert r.status_code==201,r.text;ids.append(r.json()['id'])
        tid=send(c,conv,query='结合附件分析',file_ids=ids);AIRuntime(db).run_once()
        assert c.get('/api/ai/tasks/'+tid).json()['status']=='completed',db.one('SELECT status,error FROM ai_tasks WHERE id=?',(tid,))
        assert 'ISOLATED_ATTACHMENT_TEXT' in json.dumps(seen[-1])
        assert 'input_image' in json.dumps(seen[-1])
        assert gd.one('SELECT count(*) n FROM conversations')['n']==1
        assert gd.one("SELECT count(*) n FROM requests WHERE status='succeeded'")['n']==4
        assert ledger_check(gd)['ok']
        assert c.delete('/api/ai/conversations/'+conv).status_code==200
        assert gd.one('SELECT count(*) n FROM requests')['n']==4
        assert ledger_check(gd)['ok']
    gc.close()

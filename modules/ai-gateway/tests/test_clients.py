import json
import os
import subprocess
import time

import httpx
from openai import OpenAI

from aigateway.app import create_app
from aigateway.core import Database, Settings, create_user
from conftest import HEADERS, PASSWORD, chat_result, configured
from live import serve


def events(items):
    return ''.join('event: '+e['type']+'\ndata: '+json.dumps({**e,'sequence_number':i})+'\n\n' for i,e in enumerate(items))


def response_stream(text, item=None):
    item_id='msg-local-qa'
    final={'id':item_id,'type':'message','role':'assistant','status':'completed',
           'content':[{'type':'output_text','text':text,'annotations':[]}]}
    raw={'id':'resp-local-qa','object':'response','created_at':int(time.time()),'status':'completed','model':'upstream-model',
         'output':[item or final],'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120,
              'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
    result=[{'type':'response.created','response':{**raw,'status':'in_progress','output':[]}},
            {'type':'response.in_progress','response':{**raw,'status':'in_progress','output':[]}}]
    if item:
        result += [{'type':'response.output_item.added','output_index':0,'item':{**item,'arguments':''}},
                   {'type':'response.function_call_arguments.delta','item_id':item['id'],'output_index':0,'delta':item['arguments']},
                   {'type':'response.function_call_arguments.done','item_id':item['id'],'output_index':0,'arguments':item['arguments']},
                   {'type':'response.output_item.done','output_index':0,'item':item}]
    else:
        result += [{'type':'response.output_item.added','output_index':0,'item':{**final,'status':'in_progress','content':[]}},
                   {'type':'response.content_part.added','item_id':item_id,'output_index':0,'content_index':0,
                    'part':{'type':'output_text','text':'','annotations':[]}},
                   {'type':'response.output_text.delta','item_id':item_id,'output_index':0,'content_index':0,'delta':text},
                   {'type':'response.output_text.done','item_id':item_id,'output_index':0,'content_index':0,'text':text},
                   {'type':'response.content_part.done','item_id':item_id,'output_index':0,'content_index':0,'part':final['content'][0]},
                   {'type':'response.output_item.done','output_index':0,'item':final}]
    result.append({'type':'response.completed','response':raw})
    return httpx.Response(200,content=events(result),headers={'content-type':'text/event-stream'})


def test_openai_sdk_and_actual_codex_cli(tmp_path,monkeypatch):
    settings=Settings(tmp_path/'data',tmp_path/'config/master.key',allow_private_upstreams=True,min_free_bytes=0)
    db=Database(settings);db.initialize()
    with db.transaction() as c:
        user,tenant,application=create_user(c,'admin','SDK验收',PASSWORD,admin=True)
    seen=[];tool_state={'enabled':False,'offered':False,'output_seen':False,'tool_outputs':[]}
    import aigateway.routing as routing
    original_validate=routing.validate_payload
    tool_metadata=[]
    def inspect_payload(body,protocol,model):
        tool_metadata.append([{'type':t.get('type'),'name':t.get('name')} for t in body.get('tools',[])])
        return original_validate(body,protocol,model)
    monkeypatch.setattr(routing,'validate_payload',inspect_payload)
    def handler(request):
        body=json.loads(request.content)
        seen.append({'path':request.url.path,'stream':body.get('stream'),'model':body.get('model'),
                     'session':request.headers.get('session_id'),'keys':sorted(body)})
        if request.url.path.endswith('/chat/completions'):
            return httpx.Response(200,json=chat_result())
        if request.url.path.endswith('/compact'):
            return httpx.Response(200,json={'id':'cmp-sdk','object':'response.compaction','created_at':1,
                 'output':[{'id':'cmpitem','type':'compaction','encrypted_content':'SYNTHETIC-OPAQUE-STATE'}],
                 'usage':{'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':0}}})
        if tool_state['enabled']:
            history=json.dumps(body.get('input',[]),ensure_ascii=False)
            tool_state['tool_outputs'] += [item for item in body.get('input',[]) if isinstance(item,dict) and item.get('type') in ('function_call_output','custom_tool_call_output')]
            if 'function_call_output' in history and 'LOCAL_FIXTURE_OK' in history:
                tool_state['output_seen']=True
                return response_stream('CODEX_TOOL_OK')
            if not tool_state['offered']:
                candidates=[]
                for tool in body.get('tools',[]):
                    if tool.get('type')=='namespace':
                        candidates.extend((item,tool['name']) for item in tool.get('tools',[]))
                    else:
                        candidates.append((tool,None))
                selected=next(((item,ns) for item,ns in candidates if item.get('name') in ('exec_command','shell_command','shell')),None)
                assert selected is not None, 'Codex did not advertise a local shell tool'
                definition,namespace=selected
                name=definition['name']
                arguments={'cmd':'cat fixture.txt','workdir':str(tmp_path/'client-project'),'max_output_tokens':1000} if name=='exec_command' else (
                    {'command':'cat fixture.txt','workdir':str(tmp_path/'client-project')} if name=='shell_command' else
                    {'command':['bash','-lc','cat fixture.txt'],'workdir':str(tmp_path/'client-project')})
                item={'id':'fc-local-qa','type':'function_call','call_id':'call-local-qa','name':name,'arguments':json.dumps(arguments)}
                if namespace:item['namespace']=namespace
                tool_state['offered']=True
                return response_stream('',item)
            return response_stream('CODEX_TOOL_OUTPUT_MISSING')
        return response_stream('CODEX_GATEWAY_OK')
    app=create_app(settings,upstream_transport=httpx.MockTransport(handler))
    with serve(app) as url,httpx.Client(base_url=url,headers=HEADERS,trust_env=False) as client:
        assert client.post('/api/v1/auth/login',json={'username':'admin','password':PASSWORD}).status_code==200
        g=configured({'app':app,'client':client,'db':db,'settings':settings,'tenant':tenant,'user':user,'application':application})
        sdk=OpenAI(api_key=g['token'],base_url=url+'/v1',max_retries=0,http_client=httpx.Client(trust_env=False))
        answer=sdk.chat.completions.create(model='qa-model',messages=[{'role':'user','content':'SDK合成问题'}],max_completion_tokens=64)
        assert answer.choices[0].message.content=='本地协议回答'
        with sdk.responses.stream(model='qa-model',input='合成SDK Responses输入',max_output_tokens=64,store=False) as stream:
            assert stream.get_final_response().output[0].content[0].text=='CODEX_GATEWAY_OK'
        compact=sdk.responses.compact(model='qa-model',input=[{'role':'user','content':'合成压缩上下文'}])
        assert compact.object=='response.compaction'
        project=tmp_path/'client-project';project.mkdir()
        (project/'fixture.txt').write_text('LOCAL_FIXTURE_OK\n')
        # Use an empty native config directory as well as ephemeral credentials.
        # Existing logins, provider settings and unrelated MCP servers stay isolated.
        client_config=tmp_path/'codex-config';client_config.mkdir(mode=0o700)
        base_args=['codex','--no-daemon','-a','never',
                   '-c','web_search="disabled"',
                   '-c','model_provider="qa_gateway"',
                   '-c','model="qa-model"',
                   '-c','cli_auth_credentials_store="ephemeral"',
                   '-c','model_providers.qa_gateway.name="Local Gateway QA"',
                   '-c','model_providers.qa_gateway.base_url="'+url+'/v1"',
                   '-c','model_providers.qa_gateway.env_key="AI_GATEWAY_QA_KEY"',
                   '-c','model_providers.qa_gateway.wire_api="responses"',
                   '-c','model_providers.qa_gateway.requires_openai_auth=false',
                   '-c','model_providers.qa_gateway.supports_websockets=false',
                   '-c','model_providers.qa_gateway.request_max_retries=0',
                   '-c','model_providers.qa_gateway.stream_max_retries=0',
                   'exec','--ephemeral','--skip-git-repo-check','-s','read-only','-C',str(project)]
        env={**os.environ,'CODEX_HOME':str(client_config),'AI_GATEWAY_QA_KEY':g['token'],'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'}
        last=tmp_path/'codex-last.txt'
        result=subprocess.run(base_args+['--output-last-message',str(last),'只回答 CODEX_GATEWAY_OK。不调用工具，不读文件。'],
                              env=env,capture_output=True,text=True,timeout=90,stdin=subprocess.DEVNULL,start_new_session=True)
        assert result.returncode==0,(result.stderr[-1500:],tool_metadata[-1])
        assert last.read_text().strip()=='CODEX_GATEWAY_OK'
        tool_state['enabled']=True
        tool_last=tmp_path/'codex-tool-last.txt'
        # This VM denies bwrap's loopback namespace operation. The second probe uses the parent's
        # existing full-access profile with a deterministic local provider that emits only `cat fixture.txt`.
        tool_args=base_args.copy()
        tool_args[tool_args.index('read-only')]='danger-full-access'
        result=subprocess.run(tool_args+['--output-last-message',str(tool_last),'读取当前目录的fixture.txt，然后只回答 CODEX_TOOL_OK。'],
                              env=env,capture_output=True,text=True,timeout=90,stdin=subprocess.DEVNULL,start_new_session=True)
        assert result.returncode==0,result.stderr[-1500:]
        assert tool_state['output_seen'],(result.stderr[-1500:],tool_state['tool_outputs'])
        assert tool_last.read_text().strip()=='CODEX_TOOL_OK'
        sdk.close()
        assert db.one("SELECT count(*) n FROM requests WHERE status='unknown'")['n']==0
        (tmp_path/'client-verification.json').write_text(json.dumps({'sdk':True,'codex_cli':True,'codex_tool_roundtrip':True,
                    'requests':seen,'live_models':False,'tool_probe_profile':'danger-full-access; deterministic cat fixture only',
                    'host_readonly_shell_limitation':'bwrap loopback RTM_NEWADDR denied'},ensure_ascii=False,indent=2))

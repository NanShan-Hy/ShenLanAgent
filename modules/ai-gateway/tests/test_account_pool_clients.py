"""Actual SDK/Codex -> gateway -> actual CLIProxyAPI -> controlled local Responses service."""
import json
import os

from fastapi import FastAPI,Request
from fastapi.responses import Response
import httpx
from openai import OpenAI,APIStatusError
import pytest

from aigateway.account_pool_api import PREFIX
from aigateway.billing import ledger_check
from aigateway.core import private_write,Settings,Database,create_user
from aigateway.app import create_app
from conftest import PASSWORD,HEADERS
from live import serve
from test_account_pool_process import real_backend
from live import run_cli
from test_clients import response_stream


@pytest.mark.parametrize('lite',[False,True])
def test_real_backend_multiple_credentials_sdk_codex_and_no_inference_replay(tmp_path,lite):
    settings=Settings(tmp_path/'gateway-data',tmp_path/'gateway-config/master.key',min_free_bytes=0)
    db=Database(settings);db.initialize()
    with db.transaction() as c:_,tenant,application=create_user(c,'admin','客户端验收',PASSWORD,admin=True)
    app=create_app(settings);vendor=FastAPI();seen=[];tool={'enabled':False,'sent':False,'output':False,'lite_seen':False}
    @vendor.post('/v1/responses')
    async def respond(request: Request):
        body=await request.json();seen.append({'authorization':request.headers.get('authorization'),'body':body})
        if 'QA_NO_REPLAY_FAILURE' in json.dumps(body):
            return Response(json.dumps({'error':{'message':'SYNTHETIC_FAILURE','type':'server_error'}}),status_code=503,media_type='application/json')
        if 'QA_LIMIT_ONE_ACCOUNT' in json.dumps(body):
            return Response(json.dumps({'error':{'message':'Synthetic per-account quota','code':'usage_limit_reached'}}),status_code=429,headers={'Retry-After':'3600'},media_type='application/json')
        if tool['enabled']:
            inline=[i for i in body.get('input',[]) if isinstance(i,dict) and i.get('type')=='additional_tools']
            if inline:tool['lite_seen']=True
            history=json.dumps(body.get('input',[]))
            if 'function_call_output' in history and 'ACCOUNT_POOL_FIXTURE_OK' in history:
                tool['output']=True
                return Response(response_stream('ACCOUNT_POOL_TOOL_OK').content,media_type='text/event-stream')
            if not tool['sent']:
                choices=[]
                definitions=list(body.get('tools',[]))
                for item in inline:definitions.extend(item['tools'])
                for definition in definitions:
                    if definition.get('type')=='namespace':choices.extend((item,definition['name']) for item in definition.get('tools',[]))
                    else:choices.append((definition,None))
                definition,namespace=next((f,n) for f,n in choices if f.get('name') in ('exec_command','shell_command','shell'))
                name=definition['name']
                arguments=({'cmd':'cat fixture.txt','workdir':str(tmp_path/'codex-project'),'max_output_tokens':1000} if name=='exec_command'
                    else {'command':'cat fixture.txt','workdir':str(tmp_path/'codex-project')} if name=='shell_command'
                    else {'command':['bash','-lc','cat fixture.txt'],'workdir':str(tmp_path/'codex-project')})
                item={'id':'fc-account-pool-qa','type':'function_call','call_id':'call-account-pool-qa','name':name,'arguments':json.dumps(arguments)}
                if namespace:item['namespace']=namespace
                tool['sent']=True
                return Response(response_stream('',item).content,media_type='text/event-stream')
        return Response(response_stream('ACCOUNT_POOL_CLIENT_OK').content,media_type='text/event-stream')
    with serve(vendor) as vendor_url:
        config={'api-keys':{'codex':[{'name':'local-qa-vendor','base-url':vendor_url+'/v1','proxy-url':'direct',
            'request-retry':0,'models':[{'name':'gpt-pool-qa','alias':'gpt-pool-qa'}],
            'keys':[{'api-key':'SYNTHETIC_VENDOR_ACCOUNT_A','websockets':False},{'api-key':'SYNTHETIC_VENDOR_ACCOUNT_B','websockets':False},{'api-key':'SYNTHETIC_VENDOR_ACCOUNT_C','websockets':False}]}]}}
        with real_backend(tmp_path,extra_config=config) as (_,connector,_):
            settings.account_pool_config=connector
            with serve(app) as gateway_url,httpx.Client(base_url=gateway_url,headers=HEADERS,trust_env=False) as manager:
                settings.public_url=gateway_url
                assert manager.post('/api/v1/auth/login',json={'username':'admin','password':PASSWORD}).status_code==200
                b=manager.post(PREFIX+'/bootstrap',json={'tenant_id':tenant,'sharing_consent':True}).json()
                assert manager.put('/api/v1/admin/policies/user/'+db.one("SELECT id FROM users WHERE username='admin'")['id'],json={'rpm':1000,'tpm':100000,'daily_tokens':200000,'concurrency':10}).status_code==200
                db.execute('UPDATE pools SET rpm=1000,tpm=10000000 WHERE id=?',(b['pool_id'],))
                probe=manager.post('/api/v1/admin/upstreams/'+b['upstream_id']+'/probe',json={})
                assert probe.status_code==200 and 'gpt-pool-qa' in probe.json()['models'],probe.text
                model=manager.post(PREFIX+'/models',json={'model_ids':['gpt-pool-qa'],'input_rate':1000000,'cache_rate':500000,'output_rate':2000000})
                assert model.status_code==200,model.text
                key=manager.post('/api/v1/keys',json={'name':'Two-hop QA','application_id':application,'models':['gpt/gpt-pool-qa']}).json()['key']
                order=manager.post('/api/v1/orders',json={'kind':'topup','amount_fen':1000,'client_ref':'two-hop-qa-funding'}).json()
                assert manager.post('/api/v1/admin/orders/'+order['id']+'/approve',json={'reference':'合成客户端验收，不是真实收款'}).status_code==200
                client=OpenAI(api_key=key,base_url=gateway_url+'/v1',max_retries=0,http_client=httpx.Client(trust_env=False))
                answer=client.chat.completions.create(model='gpt/gpt-pool-qa',messages=[{'role':'user','content':'Synthetic SDK chat'}])
                assert answer.choices[0].message.content=='ACCOUNT_POOL_CLIENT_OK'
                answer=client.responses.create(model='gpt/gpt-pool-qa',input='Synthetic SDK Responses')
                assert answer.output_text=='ACCOUNT_POOL_CLIENT_OK'
                assert len({r['authorization'] for r in seen})==2
                # Stateless SDK requests must not all inherit one default session.
                independent=[]
                for _ in range(6):
                    client.responses.create(model='gpt/gpt-pool-qa',input='Independent SDK task')
                    independent.append(seen[-1]['authorization'])
                assert len(set(independent))==3
                assert max(independent.count(a) for a in set(independent))-min(independent.count(a) for a in set(independent))<=1
                # Each explicit session remains sticky; new sessions distribute fairly.
                selected=[]
                for number in range(6):
                    session={'X-Session-ID':'independent-'+str(number)}
                    client.responses.create(model='gpt/gpt-pool-qa',input='New session',extra_headers=session)
                    auth=seen[-1]['authorization'];selected.append(auth)
                    client.responses.create(model='gpt/gpt-pool-qa',input='Follow-up full context',extra_headers=session)
                    assert seen[-1]['authorization']==auth
                assert len(set(selected))==3
                assert max(selected.count(a) for a in set(selected))-min(selected.count(a) for a in set(selected))<=1
                # Quota failure is not replayed; the NEXT request may rebind.
                sticky={'X-Session-ID':'quota-recovery-session'}
                client.responses.create(model='gpt/gpt-pool-qa',input='Bind session',extra_headers=sticky)
                bound=seen[-1]['authorization'];before=len(seen)
                with pytest.raises(APIStatusError):
                    client.responses.create(model='gpt/gpt-pool-qa',input='QA_LIMIT_ONE_ACCOUNT',extra_headers=sticky)
                assert len(seen)==before+1 and seen[-1]['authorization']==bound
                client.responses.create(model='gpt/gpt-pool-qa',input='Next turn with full history',extra_headers=sticky)
                assert seen[-1]['authorization']!=bound
                project=tmp_path/'codex-project';project.mkdir()
                (project/'fixture.txt').write_text('ACCOUNT_POOL_FIXTURE_OK\n')
                private_write(project/'AGENTS.md',
                    'Use these synthetic project instructions for this local protocol test.\n' * 2200,exclusive=True)
                cli_start=len(seen)
                args=['codex','--no-daemon','-a','never','-c','web_search="disabled"','-c','model="gpt/gpt-pool-qa"',
                    '-c','project_doc_max_bytes=240000',
                    '-c','model_provider="pool_qa"','-c','cli_auth_credentials_store="ephemeral"',
                    '-c','model_providers.pool_qa.name="Account Pool QA"','-c','model_providers.pool_qa.base_url="'+gateway_url+'/v1"',
                    '-c','model_providers.pool_qa.env_key="POOL_QA_CLIENT_KEY"','-c','model_providers.pool_qa.wire_api="responses"',
                    '-c','model_providers.pool_qa.requires_openai_auth=false','-c','model_providers.pool_qa.supports_websockets=false',
                    '-c','model_providers.pool_qa.request_max_retries=0','-c','model_providers.pool_qa.stream_max_retries=0',
                    'exec','--ephemeral','--skip-git-repo-check','-s','read-only','-C',str(project)]
                if lite:
                    # Synthetic metadata shape based on openai/codex 710bee73; no production config changes.
                    catalog=__import__('pathlib').Path(__file__).parent/'fixtures/codex-lite-model.json'
                    args[1:1]=['-c','model_catalog_json='+json.dumps(str(catalog))]
                output=tmp_path/'codex-answer.txt'
                result=run_cli(args+['--output-last-message',str(output),'只回答 ACCOUNT_POOL_CLIENT_OK，不调用工具。'],
                    {**os.environ,'POOL_QA_CLIENT_KEY':key,'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'},tmp_path,'two-hop-codex')
                assert result.returncode==0,result.stderr[-1000:]
                assert output.read_text().strip()=='ACCOUNT_POOL_CLIENT_OK'
                tool['enabled']=True
                tool_args=args.copy();tool_args[tool_args.index('read-only')]='danger-full-access'
                tool_output=tmp_path/'codex-tool-answer.txt'
                result=run_cli(tool_args+['--output-last-message',str(tool_output),'读取当前目录fixture.txt，然后只回答 ACCOUNT_POOL_TOOL_OK。'],
                    {**os.environ,'POOL_QA_CLIENT_KEY':key,'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'},tmp_path,'two-hop-codex-tool')
                assert result.returncode==0,result.stderr[-1000:]
                assert tool['output'] and tool_output.read_text().strip()=='ACCOUNT_POOL_TOOL_OK'
                if lite:assert tool['lite_seen'], 'Actual Codex did not emit Responses Lite additional_tools'
                assert max(len(json.dumps(item['body'],ensure_ascii=False).encode()) for item in seen[cli_start:])>131072
                tool['enabled']=False
                before=len(seen)
                with pytest.raises(APIStatusError):client.chat.completions.create(model='gpt/gpt-pool-qa',messages=[{'role':'user','content':'QA_LIMIT_ONE_ACCOUNT'}])
                assert len(seen)==before+1
                limited=seen[-1]['authorization']
                assert db.one('SELECT cooldown_until FROM pools WHERE id=?',(b['pool_id'],))['cooldown_until']==0
                answer=client.chat.completions.create(model='gpt/gpt-pool-qa',messages=[{'role':'user','content':'New independent task after one account limited'}])
                assert answer.choices[0].message.content=='ACCOUNT_POOL_CLIENT_OK'
                assert seen[-1]['authorization']!=limited
                before=len(seen)
                with pytest.raises(APIStatusError):client.chat.completions.create(model='gpt/gpt-pool-qa',messages=[{'role':'user','content':'QA_NO_REPLAY_FAILURE'}])
                assert len(seen)==before+1,'a failed inference must not be replayed on another credential'
                client.close()
    assert ledger_check(db)['ok']
    private_write(tmp_path/'client-verification.json',json.dumps({'actual_backend':True,'actual_sdk':True,'actual_codex':True,
        'actual_codex_client_tool_roundtrip':True,'responses_lite':lite,'additional_tools_seen':tool['lite_seen'],
        'user_tpm':100000,'actual_codex_request_bytes_over_131072':True,
        'multiple_independent_synthetic_credentials_selected':True,'failed_inference_physical_attempts':1,
        'single_account_quota_does_not_freeze_other_accounts':True,'limited_account_not_reused':True,
        'vendor':'controlled local Responses fixture','real_chatgpt_accounts_used':False,'real_payments':False}))

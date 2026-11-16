"""Gateway -> patched native SDK -> three controlled credentials, no real providers."""
import json

from fastapi import FastAPI,Request
from fastapi.responses import Response
import httpx

from aigateway.core import BASE,private_write
from conftest import configured
from live import serve
from test_clients import response_stream
import test_account_pool_process as backend


def test_native_adapter_pins_actual_credential_and_migrates_only_new_calls(gateway,tmp_path,monkeypatch):
    binary=BASE/'tools/cliproxyapi/8.0.72-gateway.2/cli-proxy-api'
    monkeypatch.setattr(backend,'BINARY',binary)
    g=configured(gateway);vendor=FastAPI();seen=[];failure={'auth':None}
    @vendor.post('/v1/responses')
    async def respond(request:Request):
        auth=request.headers['authorization'];seen.append(auth)
        if failure['auth']==auth:
            return Response(json.dumps({'error':{'code':'usage_limit_reached','message':'synthetic credential quota'}}),status_code=429,headers={'Retry-After':'3600'},media_type='application/json')
        return Response(response_stream('ADAPTER_OK').content,media_type='text/event-stream')
    with serve(vendor) as url:
        config={'api-keys':{'codex':[{'name':'adapter-test','base-url':url+'/v1','proxy-url':'direct','request-retry':0,
             'keys':[{'api-key':'SYNTHETIC_ADAPTER_A'},{'api-key':'SYNTHETIC_ADAPTER_B'},{'api-key':'SYNTHETIC_ADAPTER_C'}],
             'models':[{'name':'gpt-pool-qa','alias':'gpt-pool-qa'}]}]}}
        restarts=[]
        with backend.real_backend(tmp_path,extra_config=config,restart_callback=restarts.append) as (_,connector,values):
            values['pinning_supported']=True;private_write(connector,json.dumps(values));g['settings'].account_pool_config=connector
            g['db'].execute("UPDATE upstreams SET provider='account_pool',base_url=?,secret=? WHERE id=?",(values['base_url'],g['settings'].seal({'api_key':values['client_key']}),g['upstream']))
            g['db'].execute("UPDATE models SET provider='account_pool',upstream_model='gpt-pool-qa' WHERE id=?",(g['model'],))
            g['app'].state.upstream_transport=None
            def conv(task):
                r=g['client'].post('/api/v1/conversations/resolve',headers=g['headers'],json={'model':'qa-model','task_type':task});assert r.status_code==200,r.text;return r.json()
            def call(c,text):
                return g['client'].post('/v1/responses',headers={**g['headers'],'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':c['id'],'X-Gateway-Context-Revision':str(c['revision']),'Idempotency-Key':text},json={'model':'qa-model','input':text,'max_output_tokens':64})
            a=conv('soft');r=call(a,'first');assert r.status_code==200,r.text;first=seen[-1]
            assert call(conv('soft'),'second').status_code==200 and seen[-1]==first
            restarts[0]()
            assert call(conv('soft'),'after-backend-restart').status_code==200 and seen[-1]==first
            failure['auth']=first;r=call(conv('soft'),'quota-failed');assert r.status_code==429,r.text
            count=len(seen);r=call(conv('soft'),'continue-new-request');assert r.status_code==200,r.text
            assert len(seen)==count+1 and seen[-1]!=first
            assert g['client'].put('/api/v1/admin/conversation-policies',json={'application_id':g['application'],'task_type':'strict','policy':'strict'}).status_code==200
            c=conv('strict');assert call(c,'strict-first').status_code==200;bound=seen[-1]
            failure['auth']=bound;assert call(conv('strict'),'strict-quota').status_code==429
            count=len(seen);r=call(conv('strict'),'strict-wait');assert r.status_code==503,r.text;assert len(seen)==count
            assert g['db'].one("SELECT count(*) n FROM binding_events WHERE reason='account_unavailable'")['n']==1
            # Internal auth identifiers are stripped from the external response.
            assert 'x-gateway-account-id' not in r.headers

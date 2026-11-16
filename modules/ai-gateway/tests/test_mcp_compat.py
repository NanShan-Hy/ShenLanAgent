"""Actual Codex uses its own MCP client; gateway only handles the model protocol."""
import json
import os
from pathlib import Path
import sys

from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,Response
import httpx
import pytest

from conftest import configured
from live import serve,run_cli
from test_clients import response_stream

SERVER='''import json,sys,pathlib
def reply(r):
 method=r.get('method','');p=r.get('params',{})
 if 'id' not in r:return None
 if method=='initialize':v={'protocolVersion':p['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'gateway-mcp-qa','version':'1.0'}}
 elif method=='tools/list':v={'tools':[{'name':'sales_lookup','description':'Read synthetic sales evidence for acceptance','inputSchema':{'type':'object','properties':{'question':{'type':'string'}},'required':['question']},'annotations':{'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True}}]}
 elif method=='tools/call':
  pathlib.Path(sys.argv[1]).write_text('MCP_CALLED_ON_CLIENT')
  v={'content':[{'type':'text','text':'MCP_SYNTHETIC_SALES_42 source=qa-project'}]}
 else:v={}
 return {'jsonrpc':'2.0','id':r['id'],'result':v}
if __name__=='__main__':
 for line in sys.stdin:
  r=reply(json.loads(line))
  if r is not None:print(json.dumps(r),flush=True)
'''


@pytest.mark.parametrize('transport',['stdio','http'])
def test_real_codex_external_mcp_tool_roundtrip(gateway,tmp_path,transport):
    g=configured(gateway);called=tmp_path/'called.txt';server=tmp_path/'mcp_server.py';server.write_text(SERVER)
    namespace={'__name__':'test_module'};exec(compile(SERVER,str(server),'exec'),namespace)
    mcp=FastAPI()
    @mcp.post('/mcp')
    async def invoke(request:Request):
        # Reuse the exact same fixture behavior over Streamable HTTP.
        r=await request.json()
        if r.get('method')=='tools/call':called.write_text('MCP_CALLED_ON_CLIENT')
        if r.get('method')=='tools/call':
            response={'jsonrpc':'2.0','id':r['id'],'result':{'content':[{'type':'text','text':'MCP_SYNTHETIC_SALES_42 source=qa-project'}]}}
        else:response=namespace['reply'](r)
        return JSONResponse(response) if response is not None else Response(status_code=202)
    seen=[]
    def provider(request):
        body=json.loads(request.content);seen.append(body)
        if 'MCP_SYNTHETIC_SALES_42' in json.dumps(body.get('input',[])):
            return response_stream('MCP_CLIENT_FINISHED')
        tools=list(body.get('tools',[]))
        for item in body.get('input',[]):
            if isinstance(item,dict) and item.get('type')=='additional_tools':tools.extend(item.get('tools',[]))
        candidates=[]
        for tool in tools:
            if tool.get('type')=='namespace':candidates.extend((x,tool['name']) for x in tool.get('tools',[]))
            else:candidates.append((tool,None))
        found=next(((x,n) for x,n in candidates if 'sales_lookup' in x.get('name','')),None)
        if not found:
            # Save only the synthetic declaration names for actionable local diagnostics.
            (tmp_path/'tool-names.json').write_text(json.dumps([x.get('name',x.get('type')) for x,n in candidates]))
            return response_stream('MCP_TOOL_NOT_EXPOSED')
        tool,scope=found;item={'id':'fc-mcp-test','type':'function_call','call_id':'call-mcp-test','name':tool['name'],'arguments':'{"question":"sales"}'}
        if scope:item['namespace']=scope
        return response_stream('',item)
    g['app'].state.upstream_transport=httpx.MockTransport(provider)
    with serve(g['app'],lifespan='off') as url,serve(mcp) as mcp_url:
        project=tmp_path/'client-project';project.mkdir();output=tmp_path/'answer.txt'
        args=['codex','--no-daemon','-a','never','-c','web_search="disabled"','-c','model="qa-model"',
              '-c','model_provider="gateway_mcp_qa"','-c','cli_auth_credentials_store="ephemeral"',
              '-c','model_providers.gateway_mcp_qa.name="MCP acceptance"','-c','model_providers.gateway_mcp_qa.base_url="'+url+'/v1"',
              '-c','model_providers.gateway_mcp_qa.env_key="GATEWAY_MCP_QA_KEY"','-c','model_providers.gateway_mcp_qa.wire_api="responses"',
              '-c','model_providers.gateway_mcp_qa.requires_openai_auth=false','-c','model_providers.gateway_mcp_qa.supports_websockets=false',
              '-c','model_providers.gateway_mcp_qa.request_max_retries=0','-c','model_providers.gateway_mcp_qa.stream_max_retries=0']
        if transport=='stdio':args.extend(['-c','mcp_servers.auditlab.command='+json.dumps(sys.executable),'-c','mcp_servers.auditlab.args='+json.dumps([str(server),str(called)])])
        else:args.extend(['-c','mcp_servers.auditlab.url='+json.dumps(mcp_url+'/mcp')])
        args.extend(['exec','--ephemeral','--skip-git-repo-check','-s','danger-full-access','-C',str(project),'--output-last-message',str(output),'Call auditlab sales_lookup for sales evidence, then answer MCP_CLIENT_FINISHED.'])
        result=run_cli(args,{**os.environ,'GATEWAY_MCP_QA_KEY':g['token'],'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'},tmp_path,'mcp-client')
        assert result.returncode==0,result.stderr[-1200:]
        assert called.exists() and output.read_text().strip()=='MCP_CLIENT_FINISHED'
        assert len(seen)==2

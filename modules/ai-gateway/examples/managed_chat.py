"""Run on the integrating application's backend. MCP execution remains its responsibility."""
import argparse
import os
import uuid

import httpx
from openai import OpenAI


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('question');p.add_argument('--file');p.add_argument('--new',action='store_true');p.add_argument('--task',default='general_chat');args=p.parse_args()
    base=os.environ.get('AI_GATEWAY_BASE_URL','http://127.0.0.1:8094/v1').rstrip('/')
    token=os.environ['AI_GATEWAY_API_KEY'];model=os.environ['AI_GATEWAY_MODEL']
    identity={'X-Gateway-Authorization-Version':os.environ.get('AI_GATEWAY_AUTHORIZATION_VERSION','1')}
    if os.environ.get('AI_GATEWAY_SUBJECT'):identity['X-Gateway-Subject']=os.environ['AI_GATEWAY_SUBJECT']
    with httpx.Client(trust_env=False,timeout=60,headers={'Authorization':'Bearer '+token,**identity}) as control:
        response=control.post(base.removesuffix('/v1')+'/api/v1/conversations/resolve',json={'model':model,'protocol':'responses','task_type':args.task,'new_session':args.new})
        response.raise_for_status();session=response.json()
        if session['state'] not in ('active',):raise RuntimeError('会话需要工具结果或明确恢复，请先查询状态')
        client=OpenAI(api_key=token,base_url=base,max_retries=0,http_client=httpx.Client(trust_env=False),default_headers=identity)
        content=[{'type':'input_text','text':args.question}]
        if args.file:
            with open(args.file,'rb') as source:
                file=client.files.create(file=source,purpose='user_data')
            content.append({'type':'input_file','file_id':file.id})
        try:
            result=client.responses.with_raw_response.create(model=model,input=[{'role':'user','content':content}],max_output_tokens=1024,
                extra_headers={'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':session['id'],
                    'X-Gateway-Context-Revision':str(session['revision']),'Idempotency-Key':uuid.uuid4().hex})
            output=result.parse();print(output.output_text)
            print('conversation_id='+session['id']+' revision='+result.headers.get('x-gateway-context-revision','?'))
            if result.headers.get('x-gateway-context-warning'):raise RuntimeError('请保存本次结果并明确恢复会话，不要重复生成')
        finally:client.close()


if __name__=='__main__':main()

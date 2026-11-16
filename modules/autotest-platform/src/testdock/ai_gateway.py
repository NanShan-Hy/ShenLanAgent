"""Small bounded HTTP/SSE client. Never retries generation automatically."""
import json
from urllib.parse import urlparse
import httpx
from fastapi import HTTPException
from .ai_store import config
from .ai_sources import subject


class GatewayFailure(Exception):
    def __init__(self,message,unknown=False):super().__init__(message);self.unknown=unknown


def validate_url(value,workbench=False):
    p=urlparse(value)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise HTTPException(422,'需要无凭据的HTTP/HTTPS服务地址')
    if p.scheme=='http' and p.hostname not in ('127.0.0.1','localhost','::1'):
        raise HTTPException(422,'HTTP服务仅支持本机回环地址，远程服务请使用HTTPS')
    if not workbench and p.path.rstrip('/')!='/v1':raise HTTPException(422,'网关Base URL应以/v1结尾')
    if workbench and p.path.rstrip('/'):raise HTTPException(422,'知识工作台地址不能包含路径')
    return value.rstrip('/')


class Gateway:
    def __init__(self,db,uid,epoch=1):
        self.db=db;self.config=config(db);self.uid=uid;self.epoch=epoch
        self.base=self.config['base_url'].rstrip('/')
        self.headers={'Authorization':'Bearer '+self.config['api_key'],'X-Gateway-Subject':subject(db,uid),
                      'X-Gateway-Authorization-Version':str(epoch)}

    def request(self,method,path,body=None,files=None):
        url=self.base.removesuffix('/v1')+path
        try:
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=30) as client:
                with client.stream(method,url,headers=self.headers,json=body if files is None else None,
                                   files=files,data={'purpose':'user_data'} if files else None) as r:
                    if r.status_code>=400:self.reject(r.status_code)
                    chunks=[];size=0
                    for part in r.iter_bytes():
                        size+=len(part)
                        if size>20*1024**2:raise GatewayFailure('网关响应超过限制')
                        chunks.append(part)
            return json.loads(b''.join(chunks))
        except (httpx.HTTPError,ValueError):raise GatewayFailure('网关不可达或响应无效')

    def reject(self,status):
        label={400:'模型能力或请求格式不支持',401:'应用Key无效或过期',402:'网关消费额度不足',403:'模型、应用或身份权限不足',
               404:'模型或会话不存在',409:'会话版本冲突，请查询状态后恢复',429:'调用配额或速率受限',
               507:'网关会话或附件存储不足'}.get(status,'上游调用失败，结果可能未知')
        raise GatewayFailure('HTTP '+str(status)+'：'+label,status>=500 and status!=507)

    def generate(self,conversation,items,tools,idempotency,on_delta,check,max_output=None):
        headers={**self.headers,'X-Gateway-Context-Mode':'managed','X-Gateway-Conversation-ID':conversation['id'],
                 'X-Gateway-Context-Revision':str(conversation['revision']),'Idempotency-Key':idempotency}
        body={'model':self.config['model'],'input':items,'tools':tools,'stream':True,'store':False,
              'max_output_tokens':max_output or self.config['max_output_tokens']}
        complete=None;headers_out={};received=0;delta=[];outputs={}
        try:
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=httpx.Timeout(120,connect=10)) as client:
                with client.stream('POST',self.base+'/responses',headers=headers,json=body) as r:
                    if r.status_code>=400:self.reject(r.status_code)
                    headers_out=dict(r.headers)
                    for line in r.iter_lines():
                        check();received+=len(line.encode())
                        if received>16*1024**2 or len(line)>2*1024**2:raise GatewayFailure('模型流响应超过限制',True)
                        if not line.startswith('data:'):continue
                        raw=line[5:].strip()
                        if raw=='[DONE]':continue
                        data=json.loads(raw);kind=data.get('type','')
                        index=data.get('output_index',0)
                        if kind in ('response.output_item.added','response.output_item.done'):
                            outputs[index]=data['item']
                        if kind=='response.output_text.delta':
                            value=data.get('delta','');delta.append(value);on_delta(value)
                            item=outputs.setdefault(index,{'type':'message','role':'assistant','content':[]})
                            ci=data.get('content_index',0)
                            while len(item.setdefault('content',[]))<=ci:item['content'].append({'type':'output_text','text':''})
                            item['content'][ci]['text']=item['content'][ci].get('text','')+value
                        if kind in ('response.function_call_arguments.delta','response.function_call_arguments.done'):
                            item=outputs.setdefault(index,{'type':'function_call'})
                            if kind.endswith('.delta'):item['arguments']=item.get('arguments','')+data.get('delta','')
                            else:item['arguments']=data.get('arguments',item.get('arguments',''))
                        if kind=='response.completed':complete=data.get('response')
                        if kind in ('error','response.failed','response.incomplete','context_save_failed'):
                            raise GatewayFailure('模型返回未完成终态，请查询会话状态',True)
            if not complete:raise GatewayFailure('连接结束但未收到完成终态',True)
            if not complete.get('output'):
                complete['output']=[outputs[n] for n in sorted(outputs)] or ([{'type':'message','content':[{'type':'output_text','text':''.join(delta)}]}] if delta else [])
            if headers_out.get('x-gateway-context-warning'):raise GatewayFailure('模型结果已收到，但上下文保存需要恢复',True)
            return complete,headers_out
        except (httpx.HTTPError,ValueError,KeyError):raise GatewayFailure('模型连接中断或结果未知，不会自动重试',True)

import json
import socket
import threading
import time
from contextlib import contextmanager
import uvicorn
from fastapi import FastAPI,Request
from aiworkbench.worker import Worker
from test_access import add

@contextmanager
def serve(app):
 sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(64)
 server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
 end=time.monotonic()+10
 while not server.started and time.monotonic()<end:time.sleep(.02)
 assert server.started
 try:yield 'http://127.0.0.1:'+str(sock.getsockname()[1])
 finally:server.should_exit=True;thread.join(10);sock.close()


def setup_model(c,url,internal=True):
 r=c.post('/api/v1/models',json={'name':'协议测试服务','base_url':url,'model':'contract-test','api_key':'test-secret-key','enabled':True,'internal':internal})
 assert r.status_code==201,r.text
 return r.json()['id']


def test_model_protocol_citations_budget_and_no_key_disclosure(env):
 db,s,client,app=env;c=client();alice=client('alice');source=add(c,'已知鉴权失败',1)
 seen=[];mock=FastAPI()
 @mock.post('/chat/completions')
 async def chat(request:Request):
  body=await request.json();seen.append(body)
  return {'choices':[{'message':{'content':'根据证据存在鉴权失败 [C1]。需要核对当前配置。'}}],'usage':{'total_tokens':123}}
 with serve(mock) as url:
  mid=setup_model(c,url)
  task=alice.post('/api/v1/tasks',json={'query':'分析鉴权失败','model_id':mid,'source_ids':[source]}).json()['id']
  assert not seen  # never execute synchronously without the worker
  Worker(s).run_once();result=alice.get('/api/v1/tasks/'+task).json()
  assert result['status']=='completed',result
  assert result['result']['used_tokens']==123
  assert 'test-secret-key' not in json.dumps(seen)
  assert 'test-secret-key' not in c.get('/api/v1/models').text
  assert b'test-secret-key' not in db.path.read_bytes()
  assert c.get('/api/v1/tasks/'+task).status_code==404
  c.delete('/api/v1/projects/1/members/2')
  assert alice.get('/api/v1/tasks/'+task).status_code==403 or alice.get('/api/v1/tasks/'+task).status_code==404


def test_external_policy_ancestor_and_invalid_citations(env):
 db,s,client,app=env;c=client();source=add(c,'本地限制资料',1)
 child=add(c,'派生资料',1,sources=[{'id':source,'version':1}],egress='external')
 c.put('/api/v1/projects/1/policy',json={'egress':'external'})
 count=[];mock=FastAPI()
 @mock.post('/chat/completions')
 def chat():
  count.append(1);return {'choices':[{'message':{'content':'错误引用 [C99]'}}]}
 with serve(mock) as url:
  mid=setup_model(c,url,False)
  task=c.post('/api/v1/tasks',json={'query':'派生资料','model_id':mid,'source_ids':[child]}).json()['id'];Worker(s).run_once()
  assert c.get('/api/v1/tasks/'+task).json()['status']=='failed' and not count
  db.execute("UPDATE records SET egress='external' WHERE project_id=1")
  task=c.post('/api/v1/tasks',json={'query':'派生资料','model_id':mid,'source_ids':[child]}).json()['id'];Worker(s).run_once()
  assert c.get('/api/v1/tasks/'+task).json()['status']=='failed' and count==[1]


def test_generated_drafts_and_memory_candidates_remain_unapplied(env):
 db,s,client,app=env;c=client();source=add(c,'订单查询接口约定',1)
 mock=FastAPI()
 @mock.post('/chat/completions')
 async def chat(request:Request):
  body=await request.json()
  if 'candidates' in body['messages'][-1]['content']:text=json.dumps({'candidates':[{'title':'订单约定','body':'订单接口有状态断言'}]})
  else:text=json.dumps({'cases':[{'name':'查询订单','steps':[{'action':'http','url':'/api/orders','assertions':[{'source':'status','expected':200}]}]}]})
  return {'choices':[{'message':{'content':text}}]}
 with serve(mock) as url:
  mid=setup_model(c,url)
  for kind in ('draft','memory_extract'):
   task=c.post('/api/v1/tasks',json={'query':'根据约定整理','kind':kind,'model_id':mid,'source_ids':[source]}).json()['id'];Worker(s).run_once()
   result=c.get('/api/v1/tasks/'+task).json();assert result['status']=='completed',result
   if kind=='draft':assert result['result']['bundle']['cases'][0]['steps'][0]['action']=='http'
   else:
    rid=result['result']['candidate_ids'][0]
    assert c.get('/api/v1/records/'+rid).json()['status']=='pending'
  s.daily_tokens=1
  task=c.post('/api/v1/tasks',json={'query':'额度检查','model_id':mid,'source_ids':[source]}).json()['id'];Worker(s).run_once()
  assert '额度' in c.get('/api/v1/tasks/'+task).json()['error']


def test_revoke_after_search_before_model_call_is_blocked(env,monkeypatch):
 import aiworkbench.worker as module
 db,s,client,app=env;c=client();alice=client('alice');source=add(c,'仅B项目报价依据',2)
 calls=[];mock=FastAPI()
 @mock.post('/chat/completions')
 def chat():calls.append(1);return {'choices':[{'message':{'content':'结果[C1]'}}]}
 original=module.search
 def revoked_search(*args,**kwargs):
  result=original(*args,**kwargs)
  db.execute('DELETE FROM members WHERE project_id=2 AND user_id=2')
  return result
 with serve(mock) as url:
  mid=setup_model(c,url);task=alice.post('/api/v1/tasks',json={'query':'仅B项目报价依据','model_id':mid}).json()['id']
  monkeypatch.setattr(module,'search',revoked_search)
  Worker(s).run_once()
  assert not calls
  assert db.one('SELECT status FROM tasks WHERE id=?',(task,))['status']=='failed'

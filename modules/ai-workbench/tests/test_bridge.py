import sys
import time
from pathlib import Path
from fastapi.testclient import TestClient
from test_models import serve


def test_real_testdock_binding_live_acl_and_one_time_codes(env,tmp_path,monkeypatch):
 sys.path.insert(0,'/home/nanshan/workspace/projects/autotest-platform/src')
 from testdock.app import create_app as td_app
 from testdock.core import Settings as TDSettings,Database as TDDatabase,password_hash as td_hash
 from testdock.cli import seed_demo
 db,s,client,app=env;key=tmp_path/'bridge.key';key.write_text('test-bridge-key-that-is-not-a-real-secret');key.chmod(0o600)
 monkeypatch.setenv('TESTDOCK_AI_BRIDGE_KEY_FILE',str(key));s.bridge_key_file=key
 settings=TDSettings(tmp_path/'tddata',tmp_path/'tdconfig/master.key',min_free_bytes=0)
 td=TDDatabase(settings);td.initialize()
 for pk,name,admin in [(1,'tdadmin',1),(2,'tdalice',0)]:td.execute('INSERT INTO users(id,username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?,?)',(pk,name,name,td_hash('td-password-123'),admin,time.time()))
 seed_demo(td);td.execute('INSERT INTO members VALUES(?,?,?)',(1,2,'viewer'))
 api=td_app(settings)
 from testdock.services import new_run
 with td.transaction() as conn:run_id=new_run(td,conn,1,{'environment_id':1,'suite_id':1},1)
 with serve(api) as url,TestClient(api,headers={'X-Requested-With':'TestDock'}) as tdc:
  s.testdock_url=url
  assert tdc.post('/api/auth/login',json={'username':'tdalice','password':'td-password-123'}).status_code==200
  code=tdc.post('/api/ai/binding-code',json={}).json()['code']
  alice=client('alice');assert alice.post('/api/v1/testdock/bind',json={'code':code}).status_code==200
  import pytest
  from fastapi import HTTPException
  from aiworkbench.access import Bridge,Access
  with pytest.raises(HTTPException):Bridge(s).call('context',{'user_id':1})
  with pytest.raises(HTTPException):Bridge(s).call('bind',{'code':'self-signed-or-guessed-code-is-not-issued-by-testdock'})
  bound=Access(db,db.one('SELECT * FROM users WHERE id=2')).bridge
  with pytest.raises(HTTPException):bound.call('context',{'user_id':1})
  assert alice.post('/api/v1/testdock/bind',json={'code':code}).status_code==403
  projects=alice.get('/api/v1/projects').json()['items'];linked=next(p for p in projects if p['external_id']==1)
  assert linked['role']=='viewer'
  assert alice.put('/api/v1/projects/'+str(linked['id'])+'/members',json={'user_id':3,'role':'admin'}).status_code==403
  from aiworkbench.worker import Worker
  sync=alice.post('/api/v1/testdock/sync',json={}).json()['id'];Worker(s).run_once()
  assert alice.get('/api/v1/tasks/'+sync).json()['result']['errors']==[]
  knowledge=alice.get('/api/v1/records?kind=knowledge&project_id='+str(linked['id'])).json()['items']
  case=next(r for r in knowledge if r['kind']=='case')
  assert 'demo-pass' not in alice.get('/api/v1/records/'+case['id']).text
  memory=alice.post('/api/v1/records',json={'kind':'memory','title':'来源用例记忆','body':'当前用例约定','sources':[{'id':case['id'],'version':case['version']}]}).json()['id']
  alice.post('/api/v1/records/'+memory+'/review',json={'action':'approve'})
  external_key=db.one('SELECT external_key FROM mirrors WHERE record_id=?',(case['id'],))['external_key']
  td.execute('UPDATE cases SET name=?,revision=revision+1 WHERE id=?',('已修改来源用例',int(external_key)))
  sync=alice.post('/api/v1/testdock/sync',json={}).json()['id'];Worker(s).run_once()
  assert alice.get('/api/v1/records/'+memory).json()['stale']
  td.execute('DELETE FROM cases WHERE id=?',(int(external_key),))
  sync=alice.post('/api/v1/testdock/sync',json={}).json()['id'];Worker(s).run_once()
  assert alice.get('/api/v1/records/'+case['id']).status_code==404
  assert alice.get('/api/v1/records/'+memory).status_code==404
  evidence=alice.post('/api/v1/testdock/evidence',json={'run_id':run_id});assert evidence.status_code==201,evidence.text
  rid=evidence.json()['id'];assert alice.get('/api/v1/records/'+rid).status_code==200
  assert 'demo-pass' not in alice.get('/api/v1/records/'+rid).text
  assert alice.put('/api/v1/projects/'+str(linked['id'])+'/subscription',json={'enabled':True}).status_code==200
  td.execute("UPDATE runs SET status='failed',finished_at=? WHERE id=?",(time.time(),run_id))
  db.execute('UPDATE subscriptions SET last_checked=?',(time.time()-31,))
  from aiworkbench.worker import Worker
  worker=Worker(s);worker.poll_failures()
  assert alice.get('/api/v1/notifications').json()['unread']==1
  db.execute('UPDATE subscriptions SET last_checked=?',(time.time()-31,));worker.poll_failures()
  assert alice.get('/api/v1/notifications').json()['unread']==1
  td.execute('DELETE FROM members WHERE project_id=1 AND user_id=2')
  assert alice.get('/api/v1/records/'+rid).status_code==404
  assert alice.get('/api/v1/notifications').json()['unread']==0
  assert not any(p['external_id']==1 for p in alice.get('/api/v1/projects').json()['items'])
  assert tdc.post('/api/integrations/ai/context',json={'user_id':1}).status_code==401


def test_automatic_analysis_and_explicit_draft_apply(env,tmp_path,monkeypatch):
 import json
 from fastapi import FastAPI,Request
 from test_models import setup_model
 from aiworkbench.worker import Worker
 sys.path.insert(0,'/home/nanshan/workspace/projects/autotest-platform/src')
 from testdock.app import create_app as td_app
 from testdock.core import Settings as TDSettings,Database as TDDatabase,password_hash as td_hash
 from testdock.cli import seed_demo
 from testdock.services import new_run
 db,s,client,app=env;key=tmp_path/'bridge.key';key.write_text('integration-contract-key');s.bridge_key_file=key
 monkeypatch.setenv('TESTDOCK_AI_BRIDGE_KEY_FILE',str(key))
 td=TDDatabase(TDSettings(tmp_path/'tddata',tmp_path/'tdconfig/key',min_free_bytes=0));td.initialize()
 td.execute('INSERT INTO users(id,username,display_name,password_hash,is_admin,created_at) VALUES(1,?,?,?,?,?)',('tdadmin','管理员',td_hash('td-password-123'),1,time.time()));seed_demo(td)
 tdapi=td_app(td.settings);model=FastAPI()
 @model.post('/chat/completions')
 async def completion(request:Request):
  payload=await request.json()
  if '最多5个用例' in payload['messages'][-1]['content']:
   text=json.dumps({'cases':[{'name':'AI生成查询用例','steps':[{'action':'http','url':'/api/info','assertions':[{'source':'status','expected':200}]}]}]})
  else:text='从报告可见运行失败 [C1]，需要核对断言与环境。'
  return {'choices':[{'message':{'content':text}}],'usage':{'total_tokens':100}}
 with serve(tdapi) as url,serve(model) as modelurl,TestClient(tdapi,headers={'X-Requested-With':'TestDock'}) as tdc:
  s.testdock_url=url;tdc.post('/api/auth/login',json={'username':'tdadmin','password':'td-password-123'})
  c=client();code=tdc.post('/api/ai/binding-code',json={}).json()['code'];assert c.post('/api/v1/testdock/bind',json={'code':code}).status_code==200
  linked=next(p for p in c.get('/api/v1/projects').json()['items'] if p['external_id']==1);pid=linked['id'];mid=setup_model(c,modelurl)
  assert c.put('/api/v1/projects/'+str(pid)+'/automation',json={'enabled':True,'model_id':mid}).status_code==200
  with td.transaction() as conn:run_id=new_run(td,conn,1,{'environment_id':1,'suite_id':1},1)
  td.execute("UPDATE runs SET status='failed',finished_at=? WHERE id=?",(time.time(),run_id));db.execute('UPDATE automations SET last_checked=0')
  worker=Worker(s);worker.poll_failures();worker.poll_failures()
  assert db.one("SELECT count(*) n FROM tasks WHERE kind='analyze'")['n']==1
  worker.run_once();analysis=db.one("SELECT * FROM tasks WHERE kind='analyze'")
  result=c.get('/api/v1/tasks/'+analysis['id']).json();assert result['status']=='completed',result
  source=result['result']['record_id']
  task=c.post('/api/v1/tasks',json={'query':'根据本次报告生成用例','kind':'draft','model_id':mid,'project_ids':[pid],'source_ids':[source]}).json()['id'];worker.run_once()
  result=c.get('/api/v1/tasks/'+task).json();assert result['status']=='completed',result
  assert td.one('SELECT count(*) n FROM cases')['n']==2
  applied=c.post('/api/v1/tasks/'+task+'/apply',json={'project_id':pid});assert applied.status_code==200,applied.text
  assert td.one('SELECT count(*) n FROM cases')['n']==3
  td.execute('UPDATE users SET active=0 WHERE id=1')
  assert c.post('/api/v1/tasks/'+task+'/apply',json={'project_id':pid}).status_code in (403,404)

import json
import time
import pytest
from fastapi.testclient import TestClient
from aiworkbench.core import Settings,Database,password_hash
from aiworkbench.app import create_app

PASSWORD='test-password-123'
@pytest.fixture
def env(tmp_path):
 s=Settings(tmp_path/'data',tmp_path/'config/master.key',min_free_bytes=0)
 db=Database(s);db.initialize()
 hashed=password_hash(PASSWORD)
 for pk,name,isadmin in [(1,'admin',1),(2,'alice',0),(3,'bob',0)]:
  db.execute('INSERT INTO users(id,username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?,?)',(pk,name,name,hashed,isadmin,time.time()))
 for pid in (1,2,3):
  db.execute('INSERT INTO projects(id,name,created_at) VALUES(?,?,?)',(pid,'项目'+str(pid),time.time()))
  db.execute('INSERT INTO members VALUES(?,?,?)',(pid,1,'admin'))
 for pid,role in [(1,'editor'),(2,'viewer')]:db.execute('INSERT INTO members VALUES(?,?,?)',(pid,2,role))
 db.execute('INSERT INTO members VALUES(?,?,?)',(1,3,'viewer'))
 app=create_app(s)
 clients=[]
 def client(name='admin'):
  c=TestClient(app,headers={'X-Requested-With':'AIWorkbench'});clients.append(c)
  assert c.post('/api/v1/auth/login',json={'username':name,'password':PASSWORD}).status_code==200
  return c
 yield db,s,client,app
 for c in clients:c.close()

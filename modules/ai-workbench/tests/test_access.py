import time
from fastapi.testclient import TestClient


def add(c,title,project=1,**kwargs):
 r=c.post('/api/v1/records',json={'kind':'document','title':title,'body':title+' · 这是可检索的项目资料。','project_id':project,'visibility':'project',**kwargs})
 assert r.status_code==201,r.text
 return r.json()['id']

def test_scope_private_and_role_matrix(env):
 db,s,client,app=env;admin=client();alice=client('alice');bob=client('bob')
 a=add(admin,'A鉴权测试',1);b=add(admin,'B报价测试',2);c=add(admin,'C秘密消息',3)
 private=add(alice,'个人私有讨论',1,visibility='private')
 assert admin.get('/api/v1/records/'+private).status_code==404
 assert bob.get('/api/v1/records/'+private).status_code==404
 ids={p['id'] for p in alice.get('/api/v1/projects').json()['items']};assert ids=={1,2}
 assert alice.get('/api/v1/records/'+c).status_code==404
 assert alice.post('/api/v1/records',json={'title':'不应允许','body':'x','project_id':2,'visibility':'project'}).status_code==403
 result=alice.post('/api/v1/search',json={'query':'测试','semantic':False}).json()
 assert {i['record_id'] for i in result['items']}=={a,b}
 assert alice.post('/api/v1/search',json={'query':'秘密','project_ids':[3]}).status_code==403
 assert bob.post('/api/v1/search',json={'query':'B报价','semantic':False}).json()['items']==[]


def test_derived_cross_project_revocation_and_notifications(env):
 db,s,client,app=env;admin=client();alice=client('alice');bob=client('bob')
 a=add(admin,'A来源',1);b=add(admin,'B来源',2)
 merged=add(admin,'AB跨项目对比',1,sources=[{'id':a,'version':1},{'id':b,'version':1}],mentions=[2,3],kind='discussion')
 assert alice.get('/api/v1/records/'+merged).status_code==200
 assert bob.get('/api/v1/records/'+merged).status_code==404
 assert alice.get('/api/v1/notifications').json()['unread']==1
 assert bob.get('/api/v1/notifications').json()['unread']==0
 n=alice.get('/api/v1/notifications').json()['items'][0]['id']
 assert bob.post('/api/v1/notifications/'+str(n)+'/read',json={}).status_code==404
 assert admin.delete('/api/v1/projects/2/members/2').status_code==200
 assert alice.get('/api/v1/records/'+merged).status_code==404
 assert alice.get('/api/v1/notifications').json()['unread']==0
 assert all(i['id']!=merged for i in alice.get('/api/v1/records').json()['items'])


def test_memory_confirmation_versions_expiry_and_forgetting(env):
 db,s,client,app=env;admin=client();alice=client('alice')
 source=add(admin,'报价金额规则',1)
 memory=add(alice,'已验证的报价经验',1,kind='memory',sources=[{'id':source,'version':1}])
 assert alice.post('/api/v1/search',json={'query':'已验证','semantic':False}).json()['items']==[]
 assert alice.post('/api/v1/records/'+memory+'/review',json={'action':'approve'}).status_code==403
 assert admin.post('/api/v1/records/'+memory+'/review',json={'action':'approve'}).status_code==200
 assert any(i['record_id']==memory for i in alice.post('/api/v1/search',json={'query':'已验证','semantic':False}).json()['items'])
 assert admin.put('/api/v1/records/'+source,json={'title':'新报价规则','body':'金额规则已变更','version':1}).status_code==200
 assert alice.get('/api/v1/records/'+memory).json()['stale']
 assert not any(i['record_id']==memory for i in alice.post('/api/v1/search',json={'query':'已验证','semantic':False}).json()['items'])
 assert admin.post('/api/v1/records/'+memory+'/review',json={'action':'approve'}).status_code==409
 assert admin.delete('/api/v1/records/'+source).status_code==200
 assert alice.get('/api/v1/records/'+memory).status_code==404
 assert not db.one('SELECT 1 FROM chunks WHERE record_id=?',(source,))


def test_private_source_not_implicitly_published(env):
 db,s,client,app=env;alice=client('alice')
 source=add(alice,'私人证据',1,visibility='private')
 r=alice.post('/api/v1/records',json={'title':'泄漏派生结果','body':'x','project_id':1,'visibility':'project','sources':[{'id':source}]})
 assert r.status_code==400
 private=add(alice,'私人记忆',None,visibility='private',kind='memory')
 assert alice.post('/api/v1/records/'+private+'/review',json={'action':'approve'}).status_code==200
 assert alice.post('/api/v1/records/'+private+'/share',json={'project_id':1}).status_code==200
 assert alice.get('/api/v1/records/'+private).json()['status']=='pending'


def test_auth_csrf_disabled_models_and_inactive_account(env):
 db,s,client,app=env;c=client();alice=client('alice')
 with TestClient(app) as anon:
  assert anon.get('/api/v1/projects').status_code==401
  assert anon.post('/api/v1/auth/login',json={'username':'admin','password':'x'}).status_code==403
 assert c.post('/api/v1/projects',headers={'Origin':'http://elsewhere'},json={'name':'x'}).status_code==403
 assert alice.post('/api/v1/tasks',json={'query':'分析','kind':'answer'}).status_code==409
 assert c.patch('/api/v1/users/1',json={'active':False}).status_code==409
 assert c.patch('/api/v1/users/2',json={'active':False}).status_code==200
 assert alice.get('/api/v1/records').status_code==401


def test_personal_conversation_ownership_and_project_roster(env):
 db,s,client,app=env;admin=client();alice=client('alice');bob=client('bob')
 convo=bob.post('/api/v1/conversations',json={'title':'Bob私人会话'}).json()['id']
 assert alice.get('/api/v1/conversations/'+convo).status_code==404
 assert admin.get('/api/v1/conversations/'+convo).status_code==404
 assert alice.post('/api/v1/records',json={'kind':'chat_user','title':'注入他人会话','body':'不应允许','thread_id':convo}).status_code==403
 mine=alice.post('/api/v1/conversations',json={'title':'我的会话'}).json()['id']
 assert alice.post('/api/v1/records',json={'kind':'chat_user','title':'本人消息','body':'仅本人可见','thread_id':mine}).status_code==201
 assert len(alice.get('/api/v1/conversations/'+mine).json())==1
 assert bob.get('/api/v1/projects/1/available-users').status_code==403


def test_workspace_review_and_last_project_admin_guard(env):
 db,s,client,app=env;c=client()
 c.post('/api/v1/users',json={'username':'admin2','display_name':'第二管理员','password':'test-password-123','is_admin':True})
 reviewer=client('admin2')
 memory=add(c,'工作区通用经验',None,kind='memory',visibility='workspace')
 assert reviewer.post('/api/v1/records/'+memory+'/review',json={'action':'approve'}).status_code==200
 assert client('bob').get('/api/v1/records/'+memory).status_code==200
 assert reviewer.put('/api/v1/records/'+memory+'/policy',json={'egress':'external'}).status_code==200
 assert reviewer.put('/api/v1/records/'+memory,json={'title':'工作区通用经验','body':'管理员审核修订后的经验','version':1}).status_code==200
 assert reviewer.post('/api/v1/records/'+memory+'/review',json={'action':'approve'}).status_code==200
 assert c.delete('/api/v1/projects/1/members/1').status_code==409
 assert c.patch('/api/v1/users/1',json={'active':False}).status_code==409
 assert c.put('/api/v1/projects/1/members',json={'user_id':2,'role':'admin'}).status_code==200
 assert c.delete('/api/v1/projects/1/members/1').status_code==200

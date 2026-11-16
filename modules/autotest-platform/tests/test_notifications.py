import base64
import hashlib
import hmac
import json
import sqlite3
import time
from urllib.parse import parse_qs
import httpx
import pytest
from testdock.core import Settings,Database,SCHEMA,password_hash
from testdock.cli import seed_demo
from testdock.worker import Worker,scheduler_tick
from testdock.notifier import Notifier,Provider
from testdock.notifications import finish_run,message
from testdock.services import new_run

KEY='SCT'+'testsendkey1234567890'*2

def channel(client,kind='serverchan',enabled=True):
 cred={'serverchan':{'sendkey':KEY},'dingtalk_app':{'app_key':'ding-test-app','app_secret':'secret-application-key','robot_code':'ding-robot','conversation_id':'cid-test'},
       'dingtalk_webhook':{'webhook_url':'https://oapi.dingtalk.com/robot/send?access_token=token-test-only','sign_secret':'SEC-test-secret','keyword':'TestDock'}}[kind]
 r=client.post('/api/projects/1/notification-channels',json={'name':kind,'kind':kind,'credentials':cred,'enabled':enabled})
 assert r.status_code==201,r.text
 return r.json()['id']

def rule(c,channels,triggers=None):
 r=c.put('/api/projects/1/notification-rules',json={'channel_ids':channels,'triggers':triggers or ['schedule','manual','ci'],'mode':'all'})
 assert r.status_code==200,r.text

def schedule(c,**overrides):
 payload={'name':'每日双环境','suite_id':1,'environment_ids':[1,2,2],'cron':'0 9 * * *','timezone':'Asia/Shanghai'}|overrides
 r=c.post('/api/projects/1/schedules',json=payload);assert r.status_code==201,r.text
 return r.json()['id']

def terminal(db,pk,status='passed'):
 with db.transaction() as conn:
  conn.execute('UPDATE runs SET status=?,started_at=?,finished_at=?,summary=? WHERE id=?',(status,time.time()-3,time.time(),json.dumps({'total':2,status:2}),pk))
  finish_run(db,conn,pk)


def test_old_database_migration_and_legacy_schedule(tmp_path):
 s=Settings(tmp_path/'data',tmp_path/'config/key');s.prepare();db=Database(s)
 with db.connect() as conn:conn.executescript(SCHEMA)
 db.execute('INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)',('admin','管理员',password_hash('test-password-123'),1,time.time()))
 seed_demo(db)
 db.execute('INSERT INTO schedules(project_id,name,suite_id,environment_id,cron,timezone,next_run_at,created_by) VALUES(1,\'旧每日\',1,2,\'0 9 * * *\',\'Asia/Shanghai\',0,1)')
 db.initialize();db.initialize()
 assert db.one('SELECT version FROM schema_version')['version']==3
 assert json.loads(db.one('SELECT environment_ids FROM schedules')['environment_ids'])==[2]
 assert db.one('SELECT count(*) n FROM cases')['n']==2
 assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==0


def test_preview_validation_and_batch_atomicity(platform):
 c,db,s,app=platform
 r=c.post('/api/projects/1/schedules/preview',json={'cron':'0 9 * * *','timezone':'Asia/Shanghai'})
 assert r.status_code==200 and len(r.json()['times'])==5
 from datetime import datetime
 from zoneinfo import ZoneInfo
 assert all(datetime.fromtimestamp(t,ZoneInfo('Asia/Shanghai')).hour==9 for t in r.json()['times'])
 for value in [('bad','Asia/Shanghai'),('0 9 * * *','bad/zone'),('0 9 31 2 *','UTC')]:
  assert c.post('/api/projects/1/schedules/preview',json={'cron':value[0],'timezone':value[1]}).status_code==422
 assert c.post('/api/projects/1/schedules',json={'name':'x','suite_id':1,'environment_id':2,'environment_ids':[1,2],'cron':'0 9 * * *'}).status_code==422
 bad=schedule(c);good=schedule(c,name='正常计划',environment_ids=[2])
 db.execute('UPDATE schedules SET environment_ids=? WHERE id=?',('[1,999]',bad))
 now=time.time();db.execute('UPDATE schedules SET next_run_at=?',(now,));scheduler_tick(db,now);scheduler_tick(db,now)
 assert db.one('SELECT enabled FROM schedules WHERE id=?',(bad,))['enabled']==0
 assert db.one('SELECT count(*) n FROM runs')['n']==1
 assert db.one('SELECT schedule_id FROM runs')['schedule_id']==good
 assert db.one('SELECT count(*) n FROM schedule_fires')['n']==1


def test_batch_all_terminal_states_and_no_duplicates(platform):
 c,db,s,app=platform;cid=channel(c);rule(c,[cid]);sid=schedule(c)
 now=time.time();db.execute('UPDATE schedules SET next_run_at=? WHERE id=?',(now,sid));scheduler_tick(db,now)
 runs=db.all('SELECT * FROM runs');assert len(runs)==2
 terminal(db,runs[0]['id']);assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==0
 terminal(db,runs[1]['id'],'cancelled');terminal(db,runs[1]['id'],'cancelled')
 assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==1
 payload=s.unseal(db.one('SELECT payload FROM notification_deliveries')['payload'])
 assert payload['status']=='cancelled' and len(payload['environments'])==2
 assert len(c.get('/api/schedule-batches/'+runs[0]['batch_id']).json()['runs'])==2
 assert KEY not in json.dumps(payload)
 # Deleting a completed plan must not recycle its ID and confuse batch history.
 c.delete('/api/projects/1/schedules/'+str(sid));assert schedule(c)>sid


def test_real_dual_environment_execution_and_summary_send(targets):
 c,db,s,app=targets;cid=channel(c);rule(c,[cid]);sid=schedule(c)
 db.execute('UPDATE schedules SET next_run_at=? WHERE id=?',(time.time(),sid))
 w=Worker(s);assert w.run_once();assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==0
 assert w.run_once();assert db.one('SELECT status FROM schedule_batches')['status']=='passed'
 sent=[]
 def transport(request):
  sent.append(json.loads(request.content));return httpx.Response(200,json={'code':0,'data':{'pushid':'receipt-1'}})
 n=Notifier(s,Provider(httpx.MockTransport(transport)));assert n.run_once();assert not n.run_once()
 assert len(sent)==1 and '开发演示' in sent[0]['desp'] and '测试演示' in sent[0]['desp']
 assert 'demo-pass' not in json.dumps(sent) and 'snapshot' not in json.dumps(sent)
 assert db.one('SELECT status FROM notification_deliveries')['status']=='accepted'


def test_provider_contracts_signature_token_cache_and_serverchan():
 calls=[];payload={'project':'测试项目','title':'结果','status':'passed','counts':{'total':4,'passed':4},'environments':[],'url':'http://testserver/#batch='+'a'*32}
 def mock(request):
  calls.append(request)
  if request.url.path.endswith('/accessToken'):return httpx.Response(200,json={'accessToken':'mock-access','expireIn':7200})
  if 'groupMessages' in request.url.path:
   assert request.headers['x-acs-dingtalk-access-token']=='mock-access'
   return httpx.Response(200,json={'processQueryKey':'receipt-app'})
  if request.url.host=='oapi.dingtalk.com':return httpx.Response(200,json={'errcode':0})
  return httpx.Response(200,json={'code':0,'data':{'pushid':'receipt-wx'}})
 provider=Provider(httpx.MockTransport(mock));app={'app_key':'app','app_secret':'app-secret','robot_code':'robot','conversation_id':'group'}
 provider.send('dingtalk_app',app,payload);provider.send('dingtalk_app',app,payload)
 assert len([r for r in calls if r.url.path.endswith('/accessToken')])==1
 provider.send('dingtalk_webhook',{'webhook_url':'https://oapi.dingtalk.com/robot/send?access_token=example','sign_secret':'SECsecret'},payload)
 request=calls[-1];query=parse_qs(request.url.query.decode());stamp=query['timestamp'][0]
 expected=base64.b64encode(hmac.new(b'SECsecret',(stamp+'\nSECsecret').encode(),hashlib.sha256).digest()).decode()
 assert query['sign'][0]==expected
 assert provider.send('serverchan',{'sendkey':KEY},payload)=='receipt-wx'
 assert calls[-1].url.host=='sctapi.ftqq.com' and json.loads(calls[-1].content)['title'].startswith('TestDock')
 from testdock.notifier import SendError
 failed=[]
 def invalid_once(request):
  if 'groupMessages' in request.url.path and not failed:
   failed.append(1);return httpx.Response(401,json={'code':'token.invalid'})
  return mock(request)
 provider.transport=httpx.MockTransport(invalid_once)
 with pytest.raises(SendError):provider.send('dingtalk_app',app,payload)
 provider.send('dingtalk_app',app,payload)
 assert len([r for r in calls if r.url.path.endswith('/accessToken')])==2



def test_sender_retry_unknown_recovery_and_quota_failure(platform):
 c,db,s,app=platform;cid=channel(c,enabled=False)
 def trial():return c.post('/api/projects/1/notification-channels/'+str(cid)+'/test',json={}).json()['id']
 pk=trial()
 def refused(request):raise httpx.ConnectError('must not store URL with '+KEY,request=request)
 n=Notifier(s,Provider(httpx.MockTransport(refused)));n.run_once()
 row=db.one('SELECT * FROM notification_deliveries WHERE id=?',(pk,));assert row['status']=='pending' and row['attempts']==1 and row['next_attempt']>time.time()+50
 assert KEY not in row['error']
 db.execute('UPDATE notification_attempts SET created_at=?',(time.time()-10,));db.execute('UPDATE notification_deliveries SET next_attempt=0 WHERE id=?',(pk,))
 def uncertain(request):raise httpx.ReadTimeout('ambiguous '+KEY,request=request)
 Notifier(s,Provider(httpx.MockTransport(uncertain))).run_once();assert db.one('SELECT status FROM notification_deliveries WHERE id=?',(pk,))['status']=='unknown'
 db.execute("UPDATE notification_deliveries SET status='sending' WHERE id=?",(pk,));n.recover();assert db.one('SELECT status FROM notification_deliveries WHERE id=?',(pk,))['status']=='unknown'
 other=trial();db.execute('UPDATE notification_attempts SET created_at=?',(time.time()-10,))
 Notifier(s,Provider(httpx.MockTransport(lambda request:httpx.Response(200,json={'code':400,'message':KEY})))).run_once()
 assert db.one('SELECT status FROM notification_deliveries WHERE id=?',(other,))['status']=='failed'
 assert KEY not in c.get('/api/projects/1/notification-deliveries').text
 assert c.post('/api/projects/1/notification-deliveries/'+pk+'/retry',json={}).status_code==200


def test_permission_recheck_after_token_and_no_secret_disclosure(platform):
 c,db,s,app=platform;cid=channel(c,'dingtalk_app');pk=c.post('/api/projects/1/notification-channels/'+str(cid)+'/test',json={}).json()['id'];calls=[]
 def mock(request):
  calls.append(request.url.path);db.execute('UPDATE users SET active=0 WHERE id=1')
  return httpx.Response(200,json={'accessToken':'mock-token','expireIn':7200})
 Notifier(s,Provider(httpx.MockTransport(mock))).run_once()
 assert calls==['/v1.0/oauth2/accessToken']
 assert db.one('SELECT status FROM notification_deliveries WHERE id=?',(pk,))['status']=='cancelled'
 assert b'secret-application-key' not in db.path.read_bytes()


def test_notification_rbac_target_validation_and_channel_change(platform):
 c,db,s,app=platform;cid=channel(c)
 assert KEY not in c.get('/api/projects/1/notification-channels').text
 for bad in ['http://127.0.0.1/robot/send?access_token=x','https://oapi.dingtalk.com.evil/robot/send?access_token=x','https://oapi.dingtalk.com/robot/send?access_token=x&sign=old']:
  assert c.post('/api/projects/1/notification-channels',json={'name':'bad','kind':'dingtalk_webhook','credentials':{'webhook_url':bad}}).status_code==422
 assert c.put('/api/projects/1/notification-rules',json={'channel_ids':[999],'triggers':['schedule']}).status_code==400
 pk=c.post('/api/projects/1/notification-channels/'+str(cid)+'/test',json={}).json()['id']
 assert c.put('/api/projects/1/notification-channels/'+str(cid),json={'name':'关闭','kind':'serverchan','credentials':{},'enabled':False}).status_code==200
 assert db.one('SELECT status FROM notification_deliveries WHERE id=?',(pk,))['status']=='cancelled'
 db.execute('INSERT INTO users(username,display_name,password_hash,created_at) VALUES(?,?,?,?)',('reader','读者',password_hash('test-password-123'),time.time()));db.execute("INSERT INTO members VALUES(1,2,'viewer')")
 c.post('/api/auth/logout');c.post('/api/auth/login',json={'username':'reader','password':'test-password-123'})
 assert c.get('/api/projects/1/notification-channels').status_code==200
 assert c.post('/api/projects/1/notification-channels/'+str(cid)+'/test',json={}).status_code==403
 assert c.put('/api/projects/1/notification-rules',json={}).status_code==403
 assert c.post('/api/projects/1/notification-deliveries/'+pk+'/retry',json={}).status_code==403
 assert c.get('/api/projects/999/notification-deliveries').status_code in (403,404)


def test_manual_ci_queued_cancel_and_recovery_generate_once(platform):
 c,db,s,app=platform;cid=channel(c);rule(c,[cid])
 with db.transaction() as conn:
  ids=[new_run(db,conn,1,{'environment_id':1,'suite_id':1},1,trigger=t) for t in ('manual','ci')]
 assert c.post('/api/runs/'+ids[0]+'/cancel',json={}).status_code==200
 db.execute("UPDATE runs SET status='running' WHERE id=?",(ids[1],));Worker(s).recover();Worker(s).recover()
 assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==2
 rerun=c.post('/api/runs/'+ids[0]+'/rerun?failed_only=false',json={});assert rerun.status_code==201,rerun.text
 rerun_id=rerun.json()['id'];assert json.loads(db.one('SELECT notify_targets FROM runs WHERE id=?',(rerun_id,))['notify_targets'])[0]['id']==cid
 c.post('/api/runs/'+rerun_id+'/cancel',json={});assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==3
 # Historical results are never bulk-scanned or retroactively sent after a rule change.
 rule(c,[]);assert db.one('SELECT count(*) n FROM notification_deliveries')['n']==3


def test_backup_restores_disabled_channels_and_pending_work(platform,tmp_path):
 import sys
 from pathlib import Path
 sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
 from verify_backup import verify
 from testdock.cli import backup
 c,db,s,app=platform;cid=channel(c);rule(c,[cid]);schedule(c)
 c.post('/api/projects/1/notification-channels/'+str(cid)+'/test',json={})
 with db.transaction() as conn:new_run(db,conn,1,{'environment_id':1,'suite_id':1},1)
 snapshot=backup(db,tmp_path/'backups');restored=tmp_path/'restored';verify(snapshot,restored)
 with sqlite3.connect(restored/'testdock.sqlite3') as conn:
  assert conn.execute('SELECT enabled FROM schedules').fetchone()[0]==0
  assert conn.execute('SELECT enabled FROM notification_channels').fetchone()[0]==0
  assert conn.execute('SELECT status FROM runs').fetchone()[0]=='cancelled'
  assert conn.execute('SELECT status FROM notification_deliveries').fetchone()[0]=='cancelled'
 assert db.one('SELECT enabled FROM notification_channels')['enabled']==1
 assert db.one('SELECT status FROM notification_deliveries')['status']=='pending'

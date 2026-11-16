import hashlib
import json
import re
import time
import uuid
from typing import Literal
from urllib.parse import urlparse,parse_qs
from fastapi import HTTPException,Request,Query
from pydantic import Field,field_validator
from .models import Model,ScheduleInput
from .services import next_due,serialize_run
from .core import encode
from .notifications import schedule_view,enqueue

KINDS={'dingtalk_app','dingtalk_webhook','serverchan'}
SECRET_FIELDS={'app_key','app_secret','robot_code','conversation_id','cool_app_code','webhook_url','sign_secret','keyword','sendkey'}
class ChannelInput(Model):
    name:str=Field(min_length=1,max_length=100)
    kind:Literal['dingtalk_app','dingtalk_webhook','serverchan']
    credentials:dict[str,str]=Field(default_factory=dict,max_length=12)
    enabled:bool=False
    @field_validator('credentials')
    @classmethod
    def credentials_check(cls,value):
        if set(value)-SECRET_FIELDS or any(len(v)>4096 for v in value.values()):raise ValueError('渠道配置字段不正确')
        return value
class RuleInput(Model):
    channel_ids:list[int]=Field(default_factory=list,max_length=20)
    triggers:list[Literal['schedule','manual','ci']]=Field(default_factory=lambda:['schedule'],max_length=3)
    mode:Literal['all','failures']='all'
class PreviewInput(Model):
    cron:str=Field(max_length=100)
    timezone:str='Asia/Shanghai'


def validate_credentials(kind,cred):
    fields={'dingtalk_app':{'app_key','app_secret','robot_code','conversation_id','cool_app_code'},
            'dingtalk_webhook':{'webhook_url','sign_secret','keyword'},'serverchan':{'sendkey'}}[kind]
    if set(cred)-fields:raise HTTPException(422,'此渠道不支持填写这些配置字段')
    required=fields-{'cool_app_code','sign_secret','keyword'}
    if any(not cred.get(k) for k in required):raise HTTPException(422,'缺少渠道凭据或接收目标')
    if kind=='serverchan' and not re.fullmatch(r'SCT[A-Za-z0-9]{16,250}',cred['sendkey']):raise HTTPException(422,'请填写Server酱Turbo的SCT类型SendKey')
    if kind=='dingtalk_webhook':
        p=urlparse(cred['webhook_url']);q=parse_qs(p.query)
        if p.scheme!='https' or p.netloc!='oapi.dingtalk.com' or p.path!='/robot/send' or p.fragment or set(q)!={'access_token'} or len(q['access_token'])!=1 or not q['access_token'][0]:
            raise HTTPException(422,'请填写官方HTTPS钉钉Webhook地址，仅保留access_token参数')
    if any('\n' in v or '\r' in v for v in cred.values()):raise HTTPException(422,'渠道字段不能包含换行')
    return cred


def channel_view(row,db):
    return {k:row[k] for k in ('id','project_id','name','kind','owner_id','enabled','revision','created_at','updated_at')}|{'configured_fields':sorted(db.settings.unseal(row['secret']))}


def attach_notifications(api,db,scoped,resource,project_access,actor):
    def validate_channels(pid,ids):
        for pk in set(ids or []):
            if not db.one('SELECT 1 FROM notification_channels WHERE id=? AND project_id=? AND deleted=0',(pk,pid)):raise HTTPException(400,'通知渠道不存在或不属于当前项目')
    def channel(pid,pk):
        row=resource('notification_channels',pk,pid)
        if row['deleted']:raise HTTPException(404,'渠道不存在')
        return row
    def schedule_args(pid,body):
        resource('suites',body.suite_id,pid)
        for env in body.environment_ids:resource('environments',env,pid)
        validate_channels(pid,body.notification_channel_ids)
        return (body.name,body.suite_id,body.environment_id,body.cron,body.timezone,body.enabled,next_due(body.cron,body.timezone),
                encode(body.environment_ids),encode(body.notification_channel_ids) if body.notification_channel_ids is not None else None,body.notify_mode)
    @api.get('/api/projects/{project_id}/schedules')
    def schedules(project_id:int,request:Request):
        scoped(request,project_id)
        return [schedule_view(r) for r in db.all('SELECT * FROM schedules WHERE project_id=? ORDER BY id',(project_id,))]
    @api.post('/api/projects/{project_id}/schedules/preview')
    def preview(project_id:int,body:PreviewInput,request:Request):
        scoped(request,project_id,'tester')
        try:
            check=ScheduleInput(name='preview',suite_id=0,environment_id=0,cron=body.cron,timezone=body.timezone)
            cursor=time.time();result=[]
            for _ in range(5):cursor=next_due(check.cron,check.timezone,cursor);result.append(cursor)
        except (ValueError,KeyError,OverflowError):raise HTTPException(422,'无效的Cron或时区，或没有未来执行时间')
        return {'times':result,'timezone':body.timezone}
    @api.post('/api/projects/{project_id}/schedules',status_code=201)
    def create_schedule(project_id:int,body:ScheduleInput,request:Request):
        user=scoped(request,project_id,'tester');args=schedule_args(project_id,body)
        with db.transaction() as conn:
            pk=conn.execute('SELECT COALESCE(max(id),0)+1 FROM (SELECT id FROM schedules UNION ALL SELECT schedule_id id FROM schedule_batches UNION ALL SELECT schedule_id id FROM schedule_fires)').fetchone()[0]
            conn.execute('''INSERT INTO schedules(id,name,suite_id,environment_id,cron,timezone,enabled,next_run_at,environment_ids,notification_channel_ids,notify_mode,project_id,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(pk,*args,project_id,user['id']))
        db.audit(user['id'],project_id,'schedule.create',pk)
        return {'id':pk,**body.model_dump()}
    @api.put('/api/projects/{project_id}/schedules/{pk}')
    def edit_schedule(project_id:int,pk:int,body:ScheduleInput,request:Request):
        user=scoped(request,project_id,'tester');resource('schedules',pk,project_id)
        db.execute("UPDATE schedules SET name=?,suite_id=?,environment_id=?,cron=?,timezone=?,enabled=?,next_run_at=?,environment_ids=?,notification_channel_ids=?,notify_mode=?,last_error='' WHERE id=?",(*schedule_args(project_id,body),pk))
        db.audit(user['id'],project_id,'schedule.update',pk);return {'id':pk,**body.model_dump()}
    @api.delete('/api/projects/{project_id}/schedules/{pk}')
    def remove_schedule(project_id:int,pk:int,request:Request):
        user=scoped(request,project_id,'tester');resource('schedules',pk,project_id);db.execute('DELETE FROM schedules WHERE id=?',(pk,))
        db.audit(user['id'],project_id,'schedule.delete',pk);return {'ok':True}
    @api.get('/api/projects/{project_id}/schedule-batches')
    def batches(project_id:int,request:Request,page:int=Query(1,ge=1)):
        scoped(request,project_id)
        rows=db.all('SELECT id,name,status,scheduled_for,created_at,finished_at,reason FROM schedule_batches WHERE project_id=? ORDER BY scheduled_for DESC LIMIT 20 OFFSET ?',(project_id,(page-1)*20))
        return {'items':rows,'total':db.one('SELECT count(*) n FROM schedule_batches WHERE project_id=?',(project_id,))['n']}
    @api.get('/api/schedule-batches/{pk}')
    def batch_detail(pk:str,request:Request):
        row=db.one('SELECT * FROM schedule_batches WHERE id=?',(pk,))
        if not row:raise HTTPException(404,'批次不存在')
        scoped(request,row['project_id']);row.pop('notify_targets')
        from .services import RUN_COLUMNS
        row['runs']=[serialize_run(r) for r in db.all('SELECT '+RUN_COLUMNS+' FROM runs WHERE batch_id=? ORDER BY created_at,id',(pk,))]
        return row
    @api.get('/api/projects/{project_id}/notification-channels')
    def channels(project_id:int,request:Request):
        scoped(request,project_id)
        return [channel_view(r,db) for r in db.all('SELECT * FROM notification_channels WHERE project_id=? AND deleted=0 ORDER BY id',(project_id,))]
    def save_channel(pid,body,user,old=None):
        if old and old['kind']!=body.kind:raise HTTPException(400,'渠道类型不可变更，请新建渠道')
        cred=db.settings.unseal(old['secret']) if old else {}
        cred.update({k:v.strip() for k,v in body.credentials.items() if v.strip()})
        validate_credentials(body.kind,cred);now=time.time()
        identity=body.kind+':'+(cred.get('webhook_url') or cred.get('sendkey') or cred.get('app_key','')+':'+cred.get('robot_code','')+':'+cred.get('conversation_id',''))
        target_hash=hashlib.sha256(identity.encode()).hexdigest()
        if old:
            pk=old['id']
            changed=cred!=db.settings.unseal(old['secret']) or bool(old['enabled'])!=body.enabled
            with db.transaction() as c:
                c.execute('UPDATE notification_channels SET name=?,secret=?,target_hash=?,owner_id=?,enabled=?,revision=revision+?,updated_at=? WHERE id=?',
                          (body.name,db.settings.seal(cred),target_hash,user['id'],body.enabled,int(changed),now,pk))
                if changed:c.execute("UPDATE notification_deliveries SET status='cancelled',error='渠道配置或启用状态已变更',updated_at=? WHERE channel_id=? AND status='pending'",(now,pk))
        else:
            pk=db.execute('INSERT INTO notification_channels(project_id,name,kind,secret,target_hash,owner_id,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          (pid,body.name,body.kind,db.settings.seal(cred),target_hash,user['id'],body.enabled,now,now))
        db.audit(user['id'],pid,'notification.channel.save',pk)
        return channel_view(db.one('SELECT * FROM notification_channels WHERE id=?',(pk,)),db)
    @api.post('/api/projects/{project_id}/notification-channels',status_code=201)
    def new_channel(project_id:int,body:ChannelInput,request:Request):return save_channel(project_id,body,scoped(request,project_id,'admin'))
    @api.put('/api/projects/{project_id}/notification-channels/{pk}')
    def update_channel(project_id:int,pk:int,body:ChannelInput,request:Request):return save_channel(project_id,body,scoped(request,project_id,'admin'),channel(project_id,pk))
    @api.delete('/api/projects/{project_id}/notification-channels/{pk}')
    def delete_channel(project_id:int,pk:int,request:Request):
        user=scoped(request,project_id,'admin');channel(project_id,pk)
        with db.transaction() as c:
            c.execute('UPDATE notification_channels SET deleted=1,enabled=0,revision=revision+1 WHERE id=?',(pk,))
            c.execute("UPDATE notification_deliveries SET status='cancelled',error='渠道已删除',updated_at=? WHERE channel_id=? AND status='pending'",(time.time(),pk))
        db.audit(user['id'],project_id,'notification.channel.delete',pk);return {'ok':True}
    @api.post('/api/projects/{project_id}/notification-channels/{pk}/test',status_code=201)
    def test_channel(project_id:int,pk:int,request:Request):
        user=scoped(request,project_id,'admin');row=channel(project_id,pk)
        # Trial messages require an explicit click, but work for disabled channels.
        payload={'project':db.one('SELECT name FROM projects WHERE id=?',(project_id,))['name'],'title':'通知渠道连通性测试','status':'test',
                 'counts':{'total':0},'environments':[],'url':db.settings.report_base_url,'test':True}
        event='test:'+uuid.uuid4().hex
        with db.transaction() as c:enqueue(db,c,project_id,event,[{'id':pk,'revision':row['revision']}],payload)
        db.audit(user['id'],project_id,'notification.test',pk);return {'id':db.one('SELECT id FROM notification_deliveries WHERE event_key=?',(event,))['id']}
    @api.get('/api/projects/{project_id}/notification-rules')
    def rule(project_id:int,request:Request):
        scoped(request,project_id)
        row=db.one('SELECT * FROM notification_rules WHERE project_id=?',(project_id,))
        return {'channel_ids':json.loads(row['channel_ids']),'triggers':json.loads(row['triggers']),'mode':row['mode']} if row else RuleInput().model_dump()
    @api.put('/api/projects/{project_id}/notification-rules')
    def save_rule(project_id:int,body:RuleInput,request:Request):
        user=scoped(request,project_id,'admin');validate_channels(project_id,body.channel_ids)
        db.execute('INSERT INTO notification_rules VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET channel_ids=excluded.channel_ids,triggers=excluded.triggers,mode=excluded.mode',
                   (project_id,encode(list(dict.fromkeys(body.channel_ids))),encode(list(dict.fromkeys(body.triggers))),body.mode))
        db.audit(user['id'],project_id,'notification.rule.save');return body.model_dump()
    @api.get('/api/projects/{project_id}/notification-deliveries')
    def deliveries(project_id:int,request:Request,page:int=Query(1,ge=1)):
        scoped(request,project_id)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day_start=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        rows=db.all('''SELECT d.id,d.channel_id,c.name channel_name,c.kind,d.event_key,d.status,d.attempts,d.error,d.provider_id,d.next_attempt,d.created_at,d.updated_at
            FROM notification_deliveries d JOIN notification_channels c ON c.id=d.channel_id
            WHERE d.project_id=? ORDER BY d.created_at DESC LIMIT 20 OFFSET ?''',(project_id,(page-1)*20))
        return {'items':rows,'total':db.one('SELECT count(*) n FROM notification_deliveries WHERE project_id=?',(project_id,))['n'],
                'today_submitted':db.one("SELECT count(*) n FROM notification_deliveries WHERE project_id=? AND status='accepted' AND updated_at>=?",(project_id,day_start))['n']}
    @api.post('/api/projects/{project_id}/notification-deliveries/{pk}/retry')
    def retry_delivery(project_id:int,pk:str,request:Request):
        user=scoped(request,project_id,'admin');row=resource('notification_deliveries',pk,project_id);ch=channel(project_id,row['channel_id'])
        if row['status'] not in ('failed','unknown','cancelled'):raise HTTPException(409,'仅可重发失败、取消或状态未知的通知')
        if not ch['enabled'] and not db.settings.unseal(row['payload']).get('test'):raise HTTPException(409,'请先启用通知渠道')
        db.execute("UPDATE notification_deliveries SET status='pending',channel_revision=?,attempts=0,error='',next_attempt=?,updated_at=? WHERE id=?",(ch['revision'],time.time(),time.time(),pk))
        db.audit(user['id'],project_id,'notification.retry',pk);return {'ok':True}

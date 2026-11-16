"""Typed proposals; confirmation reuses native handlers inside one SQLite transaction."""
import json
import time
import uuid
from fastapi import HTTPException
from .core import encode
from .ai_sources import digest
from .models import CaseInput,SuiteInput,EnvironmentInput,ScheduleInput,RunInput,MatrixInput
from .services import next_due,TERMINAL

KINDS={'case.create','case.update','suite.create','suite.update','environment.create','environment.update',
       'schedule.create','schedule.update','schedule.enable','run.create','run.matrix','run.rerun'}
MODELS={'case':CaseInput,'suite':SuiteInput,'environment':EnvironmentInput,'schedule':ScheduleInput,
        'run.create':RunInput,'run.matrix':MatrixInput}
TABLES={'case':'cases','suite':'suites','environment':'environments','schedule':'schedules','run':'runs'}


def project_state(db,pid):
    state={}
    for table in ('cases','suites','environments','schedules','notification_channels','notification_rules'):
        rows=db.all('SELECT * FROM '+table+' WHERE project_id=? ORDER BY '+('project_id' if table=='notification_rules' else 'id'),(pid,))
        for row in rows:
            for key in ('next_run_at','last_error','created_at','updated_at'):row.pop(key,None)
        state[table]=rows
    return digest(state)


def normalize(db,kind,pid,target,args,normalized=False):
    if kind not in KINDS:raise HTTPException(422,'不支持的代办操作')
    family,operation=kind.split('.')
    before=None
    if operation in ('update','enable','rerun'):
        before=db.one('SELECT * FROM '+TABLES[family]+' WHERE id=? AND project_id=?',(target,pid))
        if not before:raise HTTPException(404,'目标资源不存在')
    if family=='environment' and (args.get('secrets') or args.get('remove_secrets')):
        raise HTTPException(422,'敏感变量请在环境设置中维护')
    if kind=='schedule.enable' and not normalized:
        if set(args)!={'enabled'} or type(args['enabled']) is not bool:raise HTTPException(422,'启停需要明确布尔值')
        value={k:before[k] for k in ('name','suite_id','environment_id','cron','timezone','enabled','notify_mode')}
        value['environment_ids']=json.loads(before['environment_ids']);value['notification_channel_ids']=json.loads(before['notification_channel_ids']) if before['notification_channel_ids'] else None
        value['enabled']=args['enabled'];args=value
    if kind=='run.rerun':
        if set(args)-{'failed_only','use_latest'} or any(type(v) is not bool for v in args.values()):raise HTTPException(422,'重跑选项无效')
        if before['status'] not in TERMINAL:raise HTTPException(409,'原执行尚未结束')
        return {'failed_only':args.get('failed_only',True),'use_latest':args.get('use_latest',False)},before
    model=MODELS.get(kind) or MODELS.get(family)
    if family=='schedule' and operation=='create':args={'enabled':False,**args}
    value=model.model_validate(args).model_dump()
    def exists(table,pk):
        if not db.one('SELECT id FROM '+table+' WHERE project_id=? AND id=?',(pid,pk)):raise HTTPException(422,'关联资源不属于目标项目：'+table)
    for pk in value.get('case_ids',[]):exists('cases',pk)
    if value.get('suite_id'):exists('suites',value['suite_id'])
    for pk in value.get('environment_ids') or ([value['environment_id']] if value.get('environment_id') else []):exists('environments',pk)
    for pk in value.get('notification_channel_ids') or []:exists('notification_channels',pk)
    return value,before


def prepare(db,user,access,conversation_id,kind,pid,target,args,refs=None,task_id=None):
    if len(encode(args).encode())>2*1024**2:raise HTTPException(413,'操作参数超过2MiB')
    access(user,pid,'tester')
    value,before=normalize(db,kind,pid,target,args)
    now=time.time();pk=uuid.uuid4().hex
    from .transfer import portable
    if before and kind.startswith('environment.'):
        from .services import serialize_environment
        before=serialize_environment(db.settings,before)
    elif before and kind=='run.rerun':before={k:before[k] for k in ('id','name','status','environment_name')}
    preview={'operation':kind,'project_id':pid,'target_id':target,'arguments':value,'before':portable(before) if before else None,
             'state_hash':project_state(db,pid),'sources':refs or []}
    if kind.startswith('schedule.'):
        times=[];at=time.time()
        for _ in range(5):at=next_due(value['cron'],value['timezone'],at);times.append(at)
        preview['next_fires']=times
        preview['notifications']=value.get('notification_channel_ids')
    db.execute('INSERT INTO ai_proposals(id,owner_id,conversation_id,task_id,project_id,kind,secret,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
               (pk,user['id'],conversation_id,task_id,pid,kind,db.settings.seal(preview),now+600,now))
    db.audit(user['id'],pid,'ai.proposal.prepare',pk+':'+kind)
    return proposal_view(db,db.one('SELECT * FROM ai_proposals WHERE id=?',(pk,)))


def proposal_view(db,row):
    data=db.settings.unseal(row['secret']);data.pop('state_hash',None)
    return {k:row[k] for k in ('id','kind','status','expires_at','project_id','task_id')}|data|{'result':db.settings.unseal(row['result']) if row['result'] else None}


def applied_view(db,row):
    value=proposal_view(db,row);value.pop('sources',None);value['before']=None
    return value


def execute_native(api,request,pid,kind,target,args):
    family,operation=kind.split('.')
    if kind=='run.rerun':path='/api/runs/{run_id}/rerun';kwargs={'run_id':target,**args}
    else:
        plural=TABLES[family];path='/api/projects/{project_id}/'+plural
        if kind=='run.matrix':path+='/matrix'
        if operation in ('update','enable'):path+='/{pk}'
        model=MODELS.get(kind) or MODELS[family]
        kwargs={'project_id':pid,'body':model.model_validate(args)}
        if operation in ('update','enable'):kwargs['pk']=target
    method='PUT' if operation in ('update','enable') else 'POST'
    endpoint=next((r.endpoint for r in api.routes if getattr(r,'path',None)==path and method in getattr(r,'methods',set())),None)
    if not endpoint:raise RuntimeError('原生业务接口未注册')
    return endpoint(request=request,**kwargs)


def confirm(api,db,request,user,access,pk,validate_sources):
    row=db.one('SELECT * FROM ai_proposals WHERE id=? AND owner_id=?',(pk,user['id']))
    if not row:raise HTTPException(404,'操作提案不存在')
    access(user,row['project_id'],'tester')
    if row['status']=='applied':return applied_view(db,row)
    data=db.settings.unseal(row['secret'])
    validate_sources(data['sources'],row['project_id'])
    with db.transaction():
        row=db.one('SELECT * FROM ai_proposals WHERE id=? AND owner_id=?',(pk,user['id']))
        if row['status']=='applied':return applied_view(db,row)
        if row['status']!='pending' or row['expires_at']<time.time():raise HTTPException(409,'提案已过期或不再待确认')
        access(user,row['project_id'],'tester')
        if project_state(db,row['project_id'])!=data['state_hash']:raise HTTPException(409,'项目配置已变化，请重新生成预览')
        args,_=normalize(db,row['kind'],row['project_id'],data['target_id'],data['arguments'],normalized=True)
        result=execute_native(api,request,row['project_id'],row['kind'],data['target_id'],args)
        db.execute("UPDATE ai_proposals SET status='applied',result=? WHERE id=?",(db.settings.seal(result),pk))
        db.audit(user['id'],row['project_id'],'ai.proposal.apply',pk+':'+row['kind'])
    return proposal_view(db,db.one('SELECT * FROM ai_proposals WHERE id=?',(pk,)))

"""Project-scoped notification snapshots, summaries and transactional outbox."""
import json
import time
import uuid
from .core import encode

TERMINAL={'passed','failed','error','cancelled'}


def authorized(conn,user_id,project_id,required='admin'):
    row=conn.execute('SELECT active,is_admin FROM users WHERE id=?',(user_id,)).fetchone()
    if not row or not row['active']:return False
    if row['is_admin']:return True
    member=conn.execute('SELECT role FROM members WHERE project_id=? AND user_id=?',(project_id,user_id)).fetchone()
    return bool(member and {'viewer':0,'tester':1,'admin':2}[member['role']] >= {'viewer':0,'tester':1,'admin':2}[required])


def targets(conn,project_id,trigger,channel_ids=None,mode=None):
    rule=conn.execute('SELECT * FROM notification_rules WHERE project_id=?',(project_id,)).fetchone()
    if channel_ids is None:
        if not rule or trigger not in json.loads(rule['triggers']):return [],mode or 'all'
        channel_ids=json.loads(rule['channel_ids'])
    selected=[]
    for pk in dict.fromkeys(channel_ids):
        row=conn.execute('SELECT * FROM notification_channels WHERE id=? AND project_id=? AND deleted=0 AND enabled=1',(pk,project_id)).fetchone()
        if row and authorized(conn,row['owner_id'],project_id):selected.append({'id':row['id'],'revision':row['revision']})
    return selected,mode or (rule['mode'] if rule else 'all')


def schedule_view(row):
    r=dict(row)
    for key in ('environment_ids','notification_channel_ids'):
        r[key]=json.loads(r[key]) if r[key] is not None else None
    return r


def enqueue(db,conn,project_id,event_key,selected,payload):
    now=time.time()
    for target in selected:
        conn.execute('''INSERT OR IGNORE INTO notification_deliveries
            (id,project_id,channel_id,channel_revision,event_key,payload,next_attempt,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)''',(uuid.uuid4().hex,project_id,target['id'],target['revision'],event_key,db.settings.seal(payload),now,now,now))


def summary(db,conn,rows,batch=None):
    project_id=rows[0]['project_id'];project=conn.execute('SELECT name FROM projects WHERE id=?',(project_id,)).fetchone()
    rank={'passed':0,'cancelled':1,'failed':2,'error':3}
    status=max((r['status'] for r in rows),key=lambda s:rank.get(s,3))
    counts={s:0 for s in ['passed','failed','error','cancelled','total']};items=[]
    for r in rows:
        stat=json.loads(r['summary'])
        for key in counts:counts[key]+=int(stat.get(key,0))
        items.append({'id':r['id'],'environment':r['environment_name'],'status':r['status'],'counts':stat,
                      'started_at':r['started_at'],'finished_at':r['finished_at'],
                      'url':db.settings.report_base_url+'/#run='+r['id']})
    return {'project':project['name'],'title':batch['name'] if batch else rows[0]['name'],
            'status':status,'counts':counts,'environments':items,
            'batch_id':batch['id'] if batch else None,
            'scheduled_for':batch['scheduled_for'] if batch else None,
            'url':db.settings.report_base_url+('/#batch='+batch['id'] if batch else '/#run='+rows[0]['id'])}


def finish_run(db,conn,run_id):
    run=conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not run or run['status'] not in TERMINAL:return
    if run['batch_id']:
        batch=conn.execute('SELECT * FROM schedule_batches WHERE id=?',(run['batch_id'],)).fetchone()
        if not batch or batch['status']!='running':return
        rows=conn.execute('SELECT * FROM runs WHERE batch_id=? ORDER BY created_at,id',(batch['id'],)).fetchall()
        if not rows or any(r['status'] not in TERMINAL for r in rows):return
        payload=summary(db,conn,rows,batch)
        conn.execute('UPDATE schedule_batches SET status=?,finished_at=? WHERE id=?',(payload['status'],time.time(),batch['id']))
        selected=json.loads(batch['notify_targets']);mode=batch['notify_mode'];event='batch:'+batch['id']
    else:
        payload=summary(db,conn,[run]);selected=json.loads(run['notify_targets']);mode=run['notify_mode'];event='run:'+run_id
    if mode=='failures' and payload['status'] not in ('failed','error'):return
    enqueue(db,conn,run['project_id'],event,selected,payload)


def message(payload):
    """Only allowlisted summary fields, never arbitrary executor strings or errors."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    def clean(value,limit=120):
        import re
        value=re.sub(r'(?i)(password|secret|token|api[_-]?key)\s*[=:：]\s*\S+',r'\1=***',str(value))
        return re.sub(r'[\r\n\x00-\x1f<>\[\]`*]',' ',value)[:limit]
    labels={'passed':'通过','failed':'失败','error':'异常','cancelled':'取消','test':'渠道测试'}
    title=('TestDock '+labels.get(payload['status'],'结果')+' · '+clean(payload['title']))[:32]
    lines=['### '+title,'项目：'+clean(payload['project']),'任务：'+clean(payload['title']),
           '状态：'+labels.get(payload['status'],'结果'),'总用例：'+str(payload['counts'].get('total',0))+
           '；通过 '+str(payload['counts'].get('passed',0))+'；失败 '+str(payload['counts'].get('failed',0))+
           '；异常 '+str(payload['counts'].get('error',0))+'；取消 '+str(payload['counts'].get('cancelled',0))]
    for item in payload.get('environments',[]):
        started=item.get('started_at');finished=item.get('finished_at')
        stamp=datetime.fromtimestamp(finished,ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') if finished else '—'
        seconds=max(0,finished-started) if started and finished else 0
        lines.append('- '+clean(item['environment'])+'：'+labels.get(item['status'],'异常')+'；结束 '+stamp+'（北京时间），耗时 '+str(round(seconds,2))+' 秒')
        lines.append('  [查看环境报告]('+item['url']+')')
    lines+=['[查看完整报告]('+payload['url']+')','详情需登录并拥有对应项目权限。']
    return title,'\n\n'.join(lines)

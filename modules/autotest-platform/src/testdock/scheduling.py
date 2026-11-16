import json
import time
import uuid
from fastapi import HTTPException
from .core import encode
from .services import next_due,new_run
from .notifications import authorized,targets


def scheduler_tick(db,now=None):
    now=time.time() if now is None else now
    with db.transaction() as c:
        due=c.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at',(now,)).fetchall()
        for row in due:
            s=dict(row);scheduled=s['next_run_at'];reason=''
            try:
                future=next_due(s['cron'],s['timezone'],now)
                c.execute('UPDATE schedules SET next_run_at=? WHERE id=?',(future,s['id']))
                if not authorized(c,s['created_by'],s['project_id'],'tester'):
                    c.execute("UPDATE schedules SET enabled=0,last_error='创建人已无执行权限' WHERE id=?",(s['id'],));continue
                if c.execute('SELECT 1 FROM schedule_fires WHERE schedule_id=? AND scheduled_for=?',(s['id'],scheduled)).fetchone():continue
                if now-scheduled>60:reason='停机期间已过期触发已跳过'
                elif c.execute("SELECT 1 FROM runs WHERE schedule_id=? AND status IN ('queued','running')",(s['id'],)).fetchone():reason='已有批次排队或运行，跳过重叠触发'
                batch_id=uuid.uuid4().hex
                if reason:
                    c.execute('INSERT OR IGNORE INTO schedule_batches(id,project_id,schedule_id,name,scheduled_for,created_at,finished_at,status,reason) VALUES(?,?,?,?,?,?,?,\'skipped\',?)',
                              (batch_id,s['project_id'],s['id'],s['name'],scheduled,now,now,reason))
                    c.execute('UPDATE schedules SET last_error=? WHERE id=?',(reason,s['id']));continue
                c.execute('SAVEPOINT schedule_enqueue')
                try:
                    selected,mode=targets(c,s['project_id'],'schedule',json.loads(s['notification_channel_ids']) if s['notification_channel_ids'] is not None else None,s['notify_mode'])
                    c.execute('INSERT INTO schedule_batches(id,project_id,schedule_id,name,scheduled_for,created_at,notify_targets,notify_mode) VALUES(?,?,?,?,?,?,?,?)',
                              (batch_id,s['project_id'],s['id'],s['name'],scheduled,now,encode(selected),mode))
                    ids=[]
                    for env in json.loads(s['environment_ids']):
                        pk=new_run(db,c,s['project_id'],{'suite_id':s['suite_id'],'environment_id':env,'case_ids':[]},s['created_by'],'schedule',schedule_id=s['id'],batch_id=batch_id)
                        ids.append(pk)
                    if not ids:raise ValueError('计划未选择环境')
                    c.execute('INSERT INTO schedule_fires VALUES(?,?,?)',(s['id'],scheduled,ids[0]))
                    c.execute("UPDATE schedules SET last_error='' WHERE id=?",(s['id'],))
                    c.execute('RELEASE schedule_enqueue')
                except Exception:
                    c.execute('ROLLBACK TO schedule_enqueue');c.execute('RELEASE schedule_enqueue');raise
            except (HTTPException,ValueError,KeyError,TypeError):
                c.execute("UPDATE schedules SET enabled=0,last_error='计划资源或时间配置失效，请编辑后重新启用' WHERE id=?",(s['id'],))
